"""Import carrier statements, match waybills, and build monthly allocation files."""
from __future__ import annotations

from copy import copy
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
import json
import os
from pathlib import Path
import re
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from config.settings import settings
from database import models
from utils.department_paths import build_department_path_map
from utils.regions import BEIJING, CHENGDU, XIAMEN_OTHER


ROOT = Path(__file__).resolve().parents[1]
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
REGION_NAMES = {
    XIAMEN_OTHER: "厦门及其他",
    CHENGDU: "成都",
    BEIJING: "北京",
}
CARRIERS = {"SF": "顺丰", "DB": "德邦", "JDL": "京东"}
OUTPUT_KINDS = {
    "department": "部门分摊",
    "project": "项目分摊",
    "detail": "分摊明细",
}
STATEMENT_SCHEMAS = {
    "SF": {
        "sheet_names": ("账单明细",),
        "waybill_headers": ("运单号码", "运单号"),
        "amount_headers": ("应付金额",),
    },
    "DB": {
        "sheet_names": ("本期快递明细",),
        "waybill_headers": ("运单号", "运单号码"),
        "amount_headers": ("剩余应还金额", "本期应收金额"),
    },
    "JDL": {
        "sheet_names": ("明细_1", "明细"),
        "waybill_headers": ("运单号", "运单号码"),
        "amount_headers": ("结算金额",),
    },
}
TEMPLATE_DIR = ROOT / "resources" / "billing_templates"
TEMPLATE_PATHS = {
    "department": TEMPLATE_DIR / "department_allocation.xlsx",
    "project": TEMPLATE_DIR / "project_allocation.xlsx",
}
TEMPLATE_HEADERS = {
    "department": ["部门代码", "承担金额", "备注"],
    "project": ["部门代码", "项目编号", "项目名称", "承担金额", "备注"],
}
TEMPLATE_SHEET_NAME = "分摊信息导入模板"
SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
SHARED_STRINGS_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings"
)
SHARED_STRINGS_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"
)


def _root(output_dir=None) -> Path:
    path = Path(output_dir or settings.billing_allocation_dir)
    return path if path.is_absolute() else ROOT / path


def _validate_scope(month: str, region_code: str, courier_type: str):
    month = str(month or "").strip()
    region_code = str(region_code or "").strip().lower()
    courier_type = str(courier_type or "").strip().upper()
    if not MONTH_PATTERN.fullmatch(month):
        raise ValueError("月份格式必须为 YYYY-MM")
    if region_code not in REGION_NAMES:
        raise ValueError("请选择厦门及其他、成都或北京区域")
    if courier_type not in CARRIERS:
        raise ValueError("快递类型必须为顺丰、德邦或京东")
    return month, region_code, courier_type


def _scope_dir(month, region_code, courier_type, output_dir=None) -> Path:
    month, region_code, courier_type = _validate_scope(month, region_code, courier_type)
    return _root(output_dir) / month / region_code / courier_type


def _manifest_path(month, region_code, courier_type, output_dir=None) -> Path:
    return _scope_dir(month, region_code, courier_type, output_dir) / "manifest.json"


def _atomic_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: dict):
    _atomic_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def load_allocation_manifest(month, region_code, courier_type, output_dir=None):
    try:
        path = _manifest_path(month, region_code, courier_type, output_dir)
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def list_allocation_manifests(month, region_code, output_dir=None):
    month = str(month or "").strip()
    region_code = str(region_code or "").strip().lower()
    if not MONTH_PATTERN.fullmatch(month) or region_code not in REGION_NAMES:
        return {}
    return {
        code: manifest
        for code in CARRIERS
        if (manifest := load_allocation_manifest(month, region_code, code, output_dir))
    }


def _normalize_header(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def _find_statement_sheet(workbook, courier_type):
    schema = STATEMENT_SCHEMAS[courier_type]
    preferred = {name: index for index, name in enumerate(schema["sheet_names"])}
    candidates = []
    for sheet in workbook.worksheets:
        for row_index in range(1, min(sheet.max_row, 30) + 1):
            headers = {
                _normalize_header(sheet.cell(row_index, column).value): column
                for column in range(1, sheet.max_column + 1)
                if _normalize_header(sheet.cell(row_index, column).value)
            }
            waybill = next((headers.get(name) for name in schema["waybill_headers"] if headers.get(name)), None)
            amount = next((headers.get(name) for name in schema["amount_headers"] if headers.get(name)), None)
            if waybill and amount:
                candidates.append((preferred.get(sheet.title, 999), row_index, sheet, headers, waybill, amount))
                break
    if not candidates:
        raise ValueError(
            f"未找到{CARRIERS[courier_type]}费用明细表，请上传该快递公司原始月结 Excel"
        )
    _, header_row, sheet, headers, waybill_column, amount_column = min(
        candidates, key=lambda item: (item[0], item[1])
    )
    return sheet, header_row, headers, waybill_column, amount_column


def _waybill(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip().upper()


def _amount(value) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    text = str(value).strip().replace(",", "").replace("¥", "").replace("￥", "")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"无法识别费用金额：{value}") from exc
    return -amount if negative else amount


def _order_waybills(order):
    values = {_waybill(order.courier_order_no)}
    try:
        extra = json.loads(order.courier_order_nos or "[]")
    except (TypeError, ValueError):
        extra = []
    if isinstance(extra, dict):
        extra = list(extra.values())
    for item in extra if isinstance(extra, list) else []:
        if isinstance(item, dict):
            item = item.get("waybill_no") or item.get("mailno") or item.get("waybillCode")
        values.add(_waybill(item))
    return {value for value in values if value}


def _orders_by_waybill(db, waybills, region_code, courier_type, month=None):
    if not waybills:
        return {}
    query = db.query(models.Order).filter(
        models.Order.region_code == region_code,
        models.Order.courier_type == courier_type,
    )
    if month and MONTH_PATTERN.fullmatch(str(month)):
        start = datetime.strptime(str(month), "%Y-%m")
        end = (
            datetime(start.year + 1, 1, 1)
            if start.month == 12
            else datetime(start.year, start.month + 1, 1)
        )
        query = query.filter(models.Order.created_at >= start, models.Order.created_at < end)
    orders = query.all()
    result = {}
    for order in orders:
        for number in _order_waybills(order):
            if number in waybills:
                result.setdefault(number, []).append(order)
    return result


def _copy_cell_style(source, target):
    target._style = copy(source._style)
    if source.has_style:
        target.font = copy(source.font)
        target.fill = copy(source.fill)
        target.border = copy(source.border)
        target.alignment = copy(source.alignment)
        target.number_format = source.number_format
        target.protection = copy(source.protection)


def _force_excel_text(cell, value):
    cell.value = "" if value is None else str(value)
    cell.data_type = "s"


def _template_column_style_ids(path: Path, column_count: int):
    with ZipFile(path, "r") as archive:
        root = ElementTree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    styles = {}
    for cell in root.iter(f"{{{SPREADSHEET_NS}}}c"):
        reference = cell.get("r") or ""
        if reference.endswith("2"):
            styles[re.sub(r"\d+$", "", reference)] = cell.get("s")
    return [styles.get(get_column_letter(column)) for column in range(1, column_count + 1)]


def _shared_string_workbook(data: bytes, column_style_ids=None) -> bytes:
    """Serialize inline strings as shared strings for strict third-party importers."""
    with ZipFile(BytesIO(data), "r") as source:
        entries = {entry.filename: (entry, source.read(entry.filename)) for entry in source.infolist()}

    shared_name = "xl/sharedStrings.xml"
    shared_values = []
    shared_indexes = {}
    if shared_name in entries:
        root = ElementTree.fromstring(entries[shared_name][1])
        for item in root.findall(f"{{{SPREADSHEET_NS}}}si"):
            value = "".join(node.text or "" for node in item.iter(f"{{{SPREADSHEET_NS}}}t"))
            if value not in shared_indexes:
                shared_indexes[value] = len(shared_values)
                shared_values.append(value)

    total_count = 0
    converted = 0
    worksheet_names = sorted(
        name for name in entries
        if name.startswith("xl/worksheets/") and name.endswith(".xml")
    )
    ElementTree.register_namespace("", SPREADSHEET_NS)
    for worksheet_name in worksheet_names:
        root = ElementTree.fromstring(entries[worksheet_name][1])
        changed = False
        for cell in root.iter(f"{{{SPREADSHEET_NS}}}c"):
            reference = cell.get("r") or ""
            if column_style_ids and worksheet_name == "xl/worksheets/sheet1.xml":
                column = re.sub(r"\d+$", "", reference)
                row = int(re.sub(r"^[A-Z]+", "", reference) or 0)
                column_index = 0
                for character in column:
                    column_index = column_index * 26 + ord(character) - 64
                if row >= 2 and 0 < column_index <= len(column_style_ids):
                    style_id = column_style_ids[column_index - 1]
                    if style_id is not None and cell.get("s") != style_id:
                        cell.set("s", style_id)
                        changed = True
            if cell.get("t") != "inlineStr":
                continue
            value = "".join(
                node.text or "" for node in cell.iter(f"{{{SPREADSHEET_NS}}}t")
            )
            if value not in shared_indexes:
                shared_indexes[value] = len(shared_values)
                shared_values.append(value)
            for child in list(cell):
                cell.remove(child)
            cell.set("t", "s")
            value_node = ElementTree.SubElement(cell, f"{{{SPREADSHEET_NS}}}v")
            value_node.text = str(shared_indexes[value])
            total_count += 1
            converted += 1
            changed = True
        if changed:
            entries[worksheet_name] = (
                entries[worksheet_name][0],
                ElementTree.tostring(root, encoding="utf-8", xml_declaration=True),
            )

    if not converted:
        return data

    shared_root = ElementTree.Element(
        f"{{{SPREADSHEET_NS}}}sst",
        {"count": str(total_count), "uniqueCount": str(len(shared_values))},
    )
    xml_space = "{http://www.w3.org/XML/1998/namespace}space"
    for value in shared_values:
        item = ElementTree.SubElement(shared_root, f"{{{SPREADSHEET_NS}}}si")
        text = ElementTree.SubElement(item, f"{{{SPREADSHEET_NS}}}t")
        if value != value.strip():
            text.set(xml_space, "preserve")
        text.text = value
    shared_data = ElementTree.tostring(
        shared_root, encoding="utf-8", xml_declaration=True,
    )

    content_types_name = "[Content_Types].xml"
    content_types = ElementTree.fromstring(entries[content_types_name][1])
    if not any(
        node.get("PartName") == "/xl/sharedStrings.xml"
        for node in content_types.findall(f"{{{CONTENT_TYPES_NS}}}Override")
    ):
        ElementTree.SubElement(
            content_types,
            f"{{{CONTENT_TYPES_NS}}}Override",
            {
                "PartName": "/xl/sharedStrings.xml",
                "ContentType": SHARED_STRINGS_CONTENT_TYPE,
            },
        )
        entries[content_types_name] = (
            entries[content_types_name][0],
            ElementTree.tostring(content_types, encoding="utf-8", xml_declaration=True),
        )

    relationships_name = "xl/_rels/workbook.xml.rels"
    relationships = ElementTree.fromstring(entries[relationships_name][1])
    if not any(
        node.get("Type") == SHARED_STRINGS_REL
        for node in relationships.findall(f"{{{PACKAGE_REL_NS}}}Relationship")
    ):
        used_ids = {node.get("Id") for node in relationships}
        index = 1
        while f"rId{index}" in used_ids:
            index += 1
        ElementTree.SubElement(
            relationships,
            f"{{{PACKAGE_REL_NS}}}Relationship",
            {"Id": f"rId{index}", "Type": SHARED_STRINGS_REL, "Target": "sharedStrings.xml"},
        )
        entries[relationships_name] = (
            entries[relationships_name][0],
            ElementTree.tostring(relationships, encoding="utf-8", xml_declaration=True),
        )

    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as destination:
        for name, (info, payload) in entries.items():
            destination.writestr(info, shared_data if name == shared_name else payload)
        if shared_name not in entries:
            destination.writestr(ZipInfo(shared_name), shared_data)
    return output.getvalue()


def _set_detail_columns(sheet, header_row, waybill_column, matches):
    sheet.insert_cols(waybill_column + 1, amount=4)
    labels = ("下单员工", "分摊方式", "归属部门", "归属项目")
    source_header = sheet.cell(header_row, waybill_column)
    for offset, label in enumerate(labels, 1):
        cell = sheet.cell(header_row, waybill_column + offset)
        _copy_cell_style(source_header, cell)
        cell.value = label
        sheet.column_dimensions[get_column_letter(waybill_column + offset)].width = (
            16 if offset == 1 else (14 if offset == 2 else 24)
        )
    for row_index, values in matches.items():
        source = sheet.cell(row_index, waybill_column)
        for offset, value in enumerate(values, 1):
            cell = sheet.cell(row_index, waybill_column + offset)
            _copy_cell_style(source, cell)
            _force_excel_text(cell, value)


def _template_workbook(kind, rows):
    path = TEMPLATE_PATHS[kind]
    if not path.is_file():
        raise RuntimeError(f"缺少{OUTPUT_KINDS[kind]}模板：{path.name}")
    workbook = load_workbook(path)
    sheet = workbook.worksheets[0]
    expected_headers = TEMPLATE_HEADERS[kind]
    actual_headers = [sheet.cell(1, column).value for column in range(1, len(expected_headers) + 1)]
    if sheet.title != TEMPLATE_SHEET_NAME or actual_headers != expected_headers:
        raise RuntimeError(f"{OUTPUT_KINDS[kind]}模板格式已变化，请重新核对薪福通原模板")
    column_style_ids = _template_column_style_ids(path, len(expected_headers))
    template_cells = [copy(sheet.cell(2, column)._style) for column in range(1, sheet.max_column + 1)]
    for other in list(workbook.worksheets[1:]):
        workbook.remove(other)
    if sheet.max_row > 1:
        sheet.delete_rows(2, sheet.max_row - 1)
    for row in rows:
        if len(row) != len(expected_headers):
            raise RuntimeError(f"{OUTPUT_KINDS[kind]}导出列数与薪福通模板不一致")
        sheet.append(["" if value is None else str(value) for value in row])
        current = sheet.max_row
        for column, style in enumerate(template_cells, 1):
            cell = sheet.cell(current, column)
            cell._style = copy(style)
            cell.number_format = "@"
            if isinstance(cell.value, str):
                cell.data_type = "s"
    output = BytesIO()
    workbook.save(output)
    return _shared_string_workbook(output.getvalue(), column_style_ids)


def _workbook_bytes(workbook):
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _keep_only_sheet(workbook, selected_sheet):
    """Keep only the carrier bill detail sheet in the exported detail workbook."""
    for sheet in list(workbook.worksheets):
        if sheet is not selected_sheet:
            workbook.remove(sheet)


def import_billing_statement(
    db, content: bytes, filename: str, month: str, region_code: str,
    courier_type: str, output_dir=None,
):
    month, region_code, courier_type = _validate_scope(month, region_code, courier_type)
    if not str(filename or "").lower().endswith(".xlsx"):
        raise ValueError("请上传 .xlsx 格式的月结费用明细")
    try:
        workbook = load_workbook(BytesIO(content))
    except Exception as exc:
        raise ValueError("无法读取 Excel，请确认文件未损坏且为 .xlsx 格式") from exc
    sheet, header_row, headers, waybill_column, amount_column = _find_statement_sheet(
        workbook, courier_type
    )

    statement_rows = []
    amount_header = _normalize_header(sheet.cell(header_row, amount_column).value)
    for row_index in range(header_row + 1, sheet.max_row + 1):
        number = _waybill(sheet.cell(row_index, waybill_column).value)
        if not number:
            continue
        amount = _amount(sheet.cell(row_index, amount_column).value)
        statement_rows.append((row_index, number, amount))
    if not statement_rows:
        raise ValueError("费用明细中没有可匹配的运单记录")

    waybills = {number for _, number, _ in statement_rows}
    order_map = _orders_by_waybill(db, waybills, region_code, courier_type, month)
    project_codes = {
        str(orders[0].project_code or "").strip()
        for orders in order_map.values() if len(orders) == 1
        and str(orders[0].cost_type or "").strip().lower() in {"project", "项目"}
    }
    projects = {
        project.project_code: project
        for project in db.query(models.Project).filter(
            models.Project.project_code.in_(project_codes)
        ).all()
    } if project_codes else {}
    project_names = {
        str(project.project_name or "").strip(): project
        for project in db.query(models.Project).filter(models.Project.enabled == True).all()
        if str(project.project_name or "").strip()
    }
    department_rows = db.query(models.Department).all()
    departments = {str(department.dept_code): department for department in department_rows}
    for department in department_rows:
        raw_id = str(department.dingtalk_dept_id or "").strip()
        if raw_id:
            departments.setdefault(raw_id, department)
    department_paths = build_department_path_map(department_rows)
    submitter_ids = {
        orders[0].created_by for orders in order_map.values()
        if len(orders) == 1 and orders[0].created_by
    }
    submitters = {
        user.id: user for user in db.query(models.User).filter(
            models.User.id.in_(submitter_ids or [-1])
        ).all()
    }

    detail_values = {}
    department_totals = {}
    project_totals = {}
    matched_rows = 0
    matched_amount = Decimal("0")
    matched_numbers = set()
    issue_counts = {}
    unmatched_numbers = set()

    for row_index, number, amount in statement_rows:
        orders = order_map.get(number) or []
        mode = project_name = department_name = ""
        issue = ""
        if not orders:
            issue = "未匹配运单"
        elif len(orders) > 1:
            issue = "运单对应多笔订单"
        else:
            order = orders[0]
            cost_type = str(order.cost_type or "department").strip().lower()
            if cost_type in {"project", "项目"}:
                source_project_name = str(order.project_name or "").strip()
                project = (
                    project_names.get(source_project_name)
                    or projects.get(str(order.project_code or "").strip())
                )
                if not project:
                    issue = "CRM中未找到项目"
                elif not str(project.dept_code or "").strip():
                    issue = "CRM项目未配置执行部门"
                elif str(project.dept_code) not in departments:
                    issue = "CRM项目部门编码无效"
                else:
                    mode = "项目分摊"
                    project_name = source_project_name or project.project_name or ""
                    department_name = project.dept_name or department_paths.get(
                        departments[str(project.dept_code)].id,
                        departments[str(project.dept_code)].dept_name,
                    )
                    key = (str(departments[str(project.dept_code)].dept_code), project.project_code, project_name)
                    project_totals[key] = project_totals.get(key, Decimal("0")) + amount
            else:
                department_code = str(order.dept_code or "").strip()
                department = departments.get(department_code)
                if not department:
                    issue = "下单部门编码无效"
                else:
                    mode = "部门分摊"
                    department_name = order.dept_name or department_paths.get(
                        department.id, department.dept_name
                    )
                    canonical_department_code = str(department.dept_code)
                    department_totals[canonical_department_code] = (
                        department_totals.get(canonical_department_code, Decimal("0")) + amount
                    )
        if issue:
            issue_counts[issue] = issue_counts.get(issue, 0) + 1
            unmatched_numbers.add(number)
            detail_values[row_index] = ("", issue, "", "")
        else:
            matched_rows += 1
            matched_amount += amount
            matched_numbers.add(number)
            submitter_name = ""
            if getattr(order, "created_by", None):
                submitter = submitters.get(order.created_by)
                submitter_name = submitter.name if submitter else ""
            detail_values[row_index] = (
                submitter_name, mode, department_name, project_name,
            )

    total_amount = sum((amount for _, _, amount in statement_rows), Decimal("0"))
    department_rows = [
        [code, f"{amount.quantize(Decimal('0.01')):.2f}", ""]
        for code, amount in sorted(department_totals.items())
    ]
    project_rows = [
        [dept_code, project_code, project_name, f"{amount.quantize(Decimal('0.01')):.2f}", ""]
        for (dept_code, project_code, project_name), amount in sorted(project_totals.items())
    ]
    _set_detail_columns(sheet, header_row, waybill_column, detail_values)
    _keep_only_sheet(workbook, sheet)

    month_key = month.replace("-", "")
    prefix = f"{month_key}{REGION_NAMES[region_code]}区域{CARRIERS[courier_type]}快递费用"
    filenames = {
        "department": f"{prefix}部门分摊.xlsx",
        "project": f"{prefix}项目分摊.xlsx",
        "detail": f"{prefix}分摊明细.xlsx",
    }
    directory = _scope_dir(month, region_code, courier_type, output_dir)
    outputs = {
        "department": _template_workbook("department", department_rows),
        "project": _template_workbook("project", project_rows),
        "detail": _workbook_bytes(workbook),
    }
    for kind, data in outputs.items():
        _atomic_bytes(directory / filenames[kind], data)
    _atomic_bytes(directory / "原始账单.xlsx", content)

    manifest = {
        "schema_version": 1,
        "month": month,
        "region_code": region_code,
        "region_name": REGION_NAMES[region_code],
        "courier_type": courier_type,
        "carrier_name": CARRIERS[courier_type],
        "source_filename": Path(filename).name,
        "detail_sheet": sheet.title,
        "amount_header": amount_header,
        "uploaded_at": datetime.now().isoformat(timespec="seconds"),
        "row_count": len(statement_rows),
        "unique_waybill_count": len(waybills),
        "matched_rows": matched_rows,
        "matched_waybill_count": len(matched_numbers),
        "unmatched_rows": len(statement_rows) - matched_rows,
        "unmatched_waybill_count": len(unmatched_numbers),
        "total_amount": float(total_amount),
        "matched_amount": float(matched_amount),
        "unmatched_amount": float(total_amount - matched_amount),
        "issue_counts": issue_counts,
        "outputs": {
            "department": {
                "filename": filenames["department"],
                "count": len(department_rows),
                "amount": float(sum(department_totals.values(), Decimal("0"))),
            },
            "project": {
                "filename": filenames["project"],
                "count": len(project_rows),
                "amount": float(sum(project_totals.values(), Decimal("0"))),
            },
            "detail": {"filename": filenames["detail"], "count": len(statement_rows)},
        },
    }
    _atomic_json(directory / "manifest.json", manifest)
    return manifest


def allocation_output_path(month, region_code, courier_type, kind, output_dir=None):
    kind = str(kind or "").strip().lower()
    if kind not in OUTPUT_KINDS:
        return None, None
    manifest = load_allocation_manifest(month, region_code, courier_type, output_dir)
    output = ((manifest or {}).get("outputs") or {}).get(kind) or {}
    filename = str(output.get("filename") or "")
    if not filename or Path(filename).name != filename:
        return None, None
    path = _scope_dir(month, region_code, courier_type, output_dir) / filename
    return (path, filename) if path.is_file() else (None, None)
