"""Persistent billing allocation workbench and manual waybill matching."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path

from openpyxl import load_workbook

from database import models
from utils.billing_allocation import (
    CARRIERS, OUTPUT_KINDS, REGION_NAMES, _amount, _atomic_bytes, _atomic_json,
    _find_statement_sheet, _manifest_path, _orders_by_waybill, _scope_dir,
    _set_detail_columns, _template_workbook, _validate_scope, _waybill,
    _workbook_bytes,
)
from utils.department_paths import build_department_path_map


MATCH_SOURCE_LABELS = {
    "system_order": "系统订单",
    "manual_waybill": "员工补充运单",
    "manual_assignment": "管理员人工指定",
}


def _json_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat(sep=" ")
    return str(value)


def _reference_maps(db):
    department_rows = db.query(models.Department).all()
    departments = {str(item.dept_code): item for item in department_rows}
    for item in department_rows:
        raw_id = str(item.dingtalk_dept_id or "").strip()
        if raw_id:
            departments.setdefault(raw_id, item)
    department_paths = build_department_path_map(department_rows)
    projects = {
        str(item.project_code): item for item in db.query(models.Project).all()
    }
    return departments, department_paths, projects


def _submitter_map(db):
    return {item.id: item for item in db.query(models.User).all()}


def _attach_submitter(result, user_id, submitters):
    """Attach the real DingTalk user; sender/contact names are never used here."""
    try:
        user_id = int(user_id) if user_id is not None else None
    except (TypeError, ValueError):
        user_id = None
    user = submitters.get(user_id) if user_id else None
    result["submitter_user_id"] = user.id if user else None
    result["submitter_name"] = str(user.name or "").strip() if user else None
    if not user or not result["submitter_name"]:
        missing = "未匹配到下单员工，请选择钉钉实名员工"
        current = str(result.get("issue") or "").strip()
        result["issue"] = f"{current}；{missing}" if current else missing
    return result


def _department_allocation(code, fallback_name, departments, department_paths):
    code = str(code or "").strip()
    department = departments.get(code)
    if not department:
        return {"issue": "部门编码在当前 CRM 部门中不存在"}
    return {
        "allocation_type": "department", "dept_code": department.dept_code,
        "dept_name": fallback_name or department_paths.get(department.id, department.dept_name),
        "project_code": None, "project_name": None, "issue": None,
    }


def _project_allocation(
    code, departments, department_paths, projects, project_name=None,
    dept_override_code=None,
):
    code = str(code or "").strip()
    source_name = str(project_name or "").strip()
    name_matches = [
        item for item in projects.values()
        if str(item.project_name or "").strip() == source_name
    ] if source_name else []
    project = next(
        (item for item in name_matches if str(item.project_code or "").strip() == code),
        None,
    )
    if project is None and len(name_matches) == 1:
        project = name_matches[0]
    if project is None:
        project = projects.get(code)
    if not project:
        return {"issue": "下单项目在当前 CRM 项目中不存在"}
    dept_code = str(dept_override_code or project.dept_code or "").strip()
    department = departments.get(dept_code)
    if not department:
        return {
            # 保留项目，让分摊界面可以在不改动 CRM 资料的前提下
            # 为本次分摊补选执行部门。allocation_type 保持为空，
            # 因此该行在补选前仍会被统计为未完成。
            "allocation_type": None, "dept_code": None, "dept_name": None,
            "project_code": str(project.project_code or code),
            "project_name": source_name or project.project_name or "",
            "issue": (
                "所选部门在当前 CRM 部门中不存在"
                if dept_override_code else "CRM 项目未配置有效的执行部门"
            ),
        }
    return {
        "allocation_type": "project", "dept_code": department.dept_code,
        "dept_name": project.dept_name or department_paths.get(department.id, department.dept_name),
        "project_code": str(project.project_code or code),
        "project_name": source_name or project.project_name or "",
        "issue": None,
    }


def resolve_waybill_allocation(
    db, waybill_no, region_code, courier_type, *, month=None,
    override=None, references=None, submitters=None,
):
    """Resolve order first, then employee manual record, unless an admin override exists."""
    departments, department_paths, projects = references or _reference_maps(db)
    submitters = submitters or _submitter_map(db)
    number = _waybill(waybill_no)

    if override:
        cost_type = str(override.get("allocation_type") or "").lower()
        if cost_type == "project":
            result = _project_allocation(
                override.get("project_code"), departments, department_paths, projects,
                dept_override_code=override.get("dept_code"),
            )
        else:
            result = _department_allocation(
                override.get("dept_code"), override.get("dept_name"),
                departments, department_paths,
            )
        result.update({
            "match_source": "manual_assignment",
            "match_reference_id": override.get("match_reference_id"),
        })
        return _attach_submitter(result, override.get("submitter_user_id"), submitters)

    orders = (_orders_by_waybill(
        db, {number}, region_code, courier_type, month,
    ).get(number) or [])
    if len(orders) > 1:
        return _attach_submitter({"issue": "运单对应多笔系统订单"}, None, submitters)
    if len(orders) == 1:
        order = orders[0]
        if str(order.cost_type or "department").strip().lower() in {"project", "项目"}:
            result = _project_allocation(
                order.project_code, departments, department_paths, projects,
                order.project_name,
            )
        else:
            result = _department_allocation(
                order.dept_code, order.dept_name, departments, department_paths,
            )
        result.update({"match_source": "system_order", "match_reference_id": order.id})
        return _attach_submitter(result, order.created_by, submitters)

    manual = db.query(models.ManualWaybillRecord).filter(
        models.ManualWaybillRecord.region_code == region_code,
        models.ManualWaybillRecord.courier_type == courier_type,
        models.ManualWaybillRecord.waybill_no == number,
        models.ManualWaybillRecord.status == "approved",
    ).all()
    if len(manual) > 1:
        return _attach_submitter({"issue": "运单对应多条员工补充记录"}, None, submitters)
    if len(manual) == 1:
        record = manual[0]
        if str(record.cost_type or "department").lower() == "project":
            result = _project_allocation(
                record.project_code, departments, department_paths, projects,
                record.project_name,
            )
        else:
            result = _department_allocation(
                record.dept_code, record.dept_name, departments, department_paths,
            )
        result.update({"match_source": "manual_waybill", "match_reference_id": record.id})
        return _attach_submitter(result, record.created_by, submitters)
    return _attach_submitter(
        {"issue": "未找到系统订单或员工补充运单"}, None, submitters,
    )


def _apply_result(line, result, updated_by=None):
    line.submitter_user_id = result.get("submitter_user_id")
    line.submitter_name = result.get("submitter_name")
    line.allocation_type = result.get("allocation_type")
    line.dept_code = result.get("dept_code")
    line.dept_name = result.get("dept_name")
    line.project_code = result.get("project_code")
    line.project_name = result.get("project_name")
    line.match_source = result.get("match_source")
    line.match_reference_id = result.get("match_reference_id")
    line.issue = result.get("issue")
    if updated_by is not None:
        line.updated_by = updated_by


def save_uploaded_statement(
    db, content: bytes, filename: str, month: str, region_code: str,
    courier_type: str, *, uploaded_by=None, output_dir=None,
):
    """Persist every bill row and preserve prior administrator decisions by waybill."""
    month, region_code, courier_type = _validate_scope(month, region_code, courier_type)
    workbook = load_workbook(BytesIO(content), data_only=False)
    sheet, header_row, _headers, waybill_column, amount_column = _find_statement_sheet(
        workbook, courier_type,
    )
    statement = db.query(models.BillingStatement).filter(
        models.BillingStatement.month == month,
        models.BillingStatement.region_code == region_code,
        models.BillingStatement.courier_type == courier_type,
    ).first()
    preserved = {}
    if statement:
        old_manual_lines = db.query(models.BillingStatementLine).filter(
            models.BillingStatementLine.statement_id == statement.id,
            models.BillingStatementLine.match_source == "manual_assignment",
        ).all()
        for old in old_manual_lines:
            preserved[old.waybill_no] = {
                "allocation_type": old.allocation_type, "dept_code": old.dept_code,
                "dept_name": old.dept_name, "project_code": old.project_code,
                "match_reference_id": old.match_reference_id,
                "submitter_user_id": old.submitter_user_id,
            }
        all_old_lines = db.query(models.BillingStatementLine).filter(
            models.BillingStatementLine.statement_id == statement.id,
        ).all()
        for old in all_old_lines:
            db.delete(old)
        db.flush()
    else:
        statement = models.BillingStatement(
            month=month, region_code=region_code, courier_type=courier_type,
            source_filename=Path(filename).name, detail_sheet=sheet.title,
            amount_header=str(sheet.cell(header_row, amount_column).value or ""),
            header_row=header_row, waybill_column=waybill_column,
            amount_column=amount_column, uploaded_by=uploaded_by,
        )
        db.add(statement)
        db.flush()

    statement.source_filename = Path(filename).name
    statement.detail_sheet = sheet.title
    statement.amount_header = str(sheet.cell(header_row, amount_column).value or "")
    statement.header_row = header_row
    statement.waybill_column = waybill_column
    statement.amount_column = amount_column
    statement.uploaded_by = uploaded_by
    statement.uploaded_at = datetime.now()

    headers = [
        str(sheet.cell(header_row, column).value or f"列{column}")
        for column in range(1, sheet.max_column + 1)
    ]
    references = _reference_maps(db)
    submitters = _submitter_map(db)
    line_count = 0
    for row_index in range(header_row + 1, sheet.max_row + 1):
        number = _waybill(sheet.cell(row_index, waybill_column).value)
        if not number:
            continue
        raw = {
            headers[column - 1]: _json_value(sheet.cell(row_index, column).value)
            for column in range(1, sheet.max_column + 1)
        }
        line = models.BillingStatementLine(
            statement_id=statement.id, row_index=row_index, waybill_no=number,
            amount=float(_amount(sheet.cell(row_index, amount_column).value)),
            raw_data=json.dumps(raw, ensure_ascii=False),
        )
        result = resolve_waybill_allocation(
            db, number, region_code, courier_type,
            month=month, override=preserved.get(number),
            references=references, submitters=submitters,
        )
        _apply_result(line, result, uploaded_by if preserved.get(number) else None)
        db.add(line)
        line_count += 1
    if not line_count:
        raise ValueError("费用明细中没有可保存的运单记录")
    db.commit()
    return regenerate_statement_outputs(db, statement.id, output_dir=output_dir)


def regenerate_statement_outputs(db, statement_id, *, output_dir=None):
    statement = db.query(models.BillingStatement).filter(
        models.BillingStatement.id == statement_id,
    ).first()
    if not statement:
        raise ValueError("费用账单不存在")
    directory = _scope_dir(
        statement.month, statement.region_code, statement.courier_type, output_dir,
    )
    source_path = directory / "原始账单.xlsx"
    if not source_path.is_file():
        raise RuntimeError("原始费用账单文件不存在，请重新上传")
    workbook = load_workbook(source_path)
    if statement.detail_sheet not in workbook.sheetnames:
        raise RuntimeError("原始费用账单的明细子表不存在，请重新上传")
    sheet = workbook[statement.detail_sheet]
    lines = db.query(models.BillingStatementLine).filter(
        models.BillingStatementLine.statement_id == statement.id,
    ).order_by(models.BillingStatementLine.row_index).all()

    # 数据库工作台是增删改后的唯一数据源。重新生成时将原始
    # 账单明细行按当前工作台数据重建，避免已删除的原始行残留在导出文件。
    raw_headers = []
    for line in lines:
        try:
            values = json.loads(line.raw_data or "{}")
        except (TypeError, ValueError):
            values = {}
        for header in values:
            if header not in raw_headers:
                raw_headers.append(header)
    if not raw_headers:
        raw_headers = [
            str(sheet.cell(statement.header_row, column).value or f"列{column}")
            for column in range(1, max(statement.waybill_column, statement.amount_column) + 1)
        ]
    original_last_row = sheet.max_row
    for offset, line in enumerate(lines, start=1):
        target_row = statement.header_row + offset
        line.row_index = target_row
        try:
            values = json.loads(line.raw_data or "{}")
        except (TypeError, ValueError):
            values = {}
        for column, header in enumerate(raw_headers, start=1):
            sheet.cell(target_row, column).value = values.get(header)
    keep_last_row = statement.header_row + len(lines)
    if original_last_row > keep_last_row:
        sheet.delete_rows(keep_last_row + 1, original_last_row - keep_last_row)
    db.flush()

    detail_values = {}
    department_totals = {}
    project_totals = {}
    issues = {}
    matched_amount = Decimal("0")
    matched_waybills = set()
    unresolved_waybills = set()
    for line in lines:
        amount = Decimal(str(line.amount or 0))
        complete_submitter = bool(line.submitter_user_id and line.submitter_name)
        if complete_submitter and line.allocation_type == "project" and line.project_code and line.dept_code:
            detail_values[line.row_index] = (
                line.submitter_name, "项目分摊",
                line.dept_name or "", line.project_name or "",
            )
            key = (line.dept_code, line.project_code, line.project_name or "")
            project_totals[key] = project_totals.get(key, Decimal("0")) + amount
        elif complete_submitter and line.allocation_type == "department" and line.dept_code:
            detail_values[line.row_index] = (
                line.submitter_name, "部门分摊", line.dept_name or "", "",
            )
            department_totals[line.dept_code] = (
                department_totals.get(line.dept_code, Decimal("0")) + amount
            )
        else:
            reason = line.issue or "需要人工指定"
            detail_values[line.row_index] = (
                line.submitter_name or "", reason, "", "",
            )
            issues[reason] = issues.get(reason, 0) + 1
            unresolved_waybills.add(line.waybill_no)
            continue
        matched_amount += amount
        matched_waybills.add(line.waybill_no)

    _set_detail_columns(
        sheet, statement.header_row, statement.waybill_column, detail_values,
    )
    for other_sheet in list(workbook.worksheets):
        if other_sheet is not sheet:
            workbook.remove(other_sheet)
    department_rows = [
        [code, f"{amount.quantize(Decimal('0.01')):.2f}", ""]
        for code, amount in sorted(department_totals.items())
    ]
    project_rows = [
        [dept_code, project_code, project_name,
         f"{amount.quantize(Decimal('0.01')):.2f}", ""]
        for (dept_code, project_code, project_name), amount in sorted(project_totals.items())
    ]
    month_key = statement.month.replace("-", "")
    prefix = (
        f"{month_key}{REGION_NAMES[statement.region_code]}区域"
        f"{CARRIERS[statement.courier_type]}快递费用"
    )
    filenames = {
        "department": f"{prefix}部门分摊.xlsx",
        "project": f"{prefix}项目分摊.xlsx",
        "detail": f"{prefix}分摊明细.xlsx",
    }
    outputs = {
        "department": _template_workbook("department", department_rows),
        "project": _template_workbook("project", project_rows),
        "detail": _workbook_bytes(workbook),
    }
    for kind, data in outputs.items():
        _atomic_bytes(directory / filenames[kind], data)

    total_amount = sum((Decimal(str(line.amount or 0)) for line in lines), Decimal("0"))
    manifest = {
        "schema_version": 2, "statement_id": statement.id,
        "month": statement.month, "region_code": statement.region_code,
        "region_name": REGION_NAMES[statement.region_code],
        "courier_type": statement.courier_type,
        "carrier_name": CARRIERS[statement.courier_type],
        "source_filename": statement.source_filename,
        "detail_sheet": statement.detail_sheet,
        "amount_header": statement.amount_header,
        "uploaded_at": statement.uploaded_at.isoformat(timespec="seconds"),
        "row_count": len(lines),
        "unique_waybill_count": len({line.waybill_no for line in lines}),
        "matched_rows": sum(
            1 for line in lines
            if (
                line.submitter_user_id and line.submitter_name
                and line.allocation_type == "department" and line.dept_code
            ) or (
                line.submitter_user_id and line.submitter_name
                and line.allocation_type == "project" and line.dept_code and line.project_code
            )
        ),
        "matched_waybill_count": len(matched_waybills),
        "unmatched_rows": sum(
            1 for line in lines
            if not line.allocation_type or not line.submitter_user_id or not line.submitter_name
        ),
        "unmatched_waybill_count": len(unresolved_waybills),
        "total_amount": float(total_amount), "matched_amount": float(matched_amount),
        "unmatched_amount": float(total_amount - matched_amount),
        "issue_counts": issues,
        "outputs": {
            "department": {"filename": filenames["department"], "count": len(department_rows),
                           "amount": float(sum(department_totals.values(), Decimal("0")))},
            "project": {"filename": filenames["project"], "count": len(project_rows),
                        "amount": float(sum(project_totals.values(), Decimal("0")))},
            "detail": {"filename": filenames["detail"], "count": len(lines)},
        },
    }
    _atomic_json(
        _manifest_path(
            statement.month, statement.region_code, statement.courier_type, output_dir,
        ), manifest,
    )
    db.commit()
    return manifest


def line_to_dict(line, statement, creator_name=None):
    try:
        raw = json.loads(line.raw_data or "{}")
    except (TypeError, ValueError):
        raw = {}
    return {
        "id": line.id, "statement_id": statement.id, "month": statement.month,
        "region_code": statement.region_code,
        "region_name": REGION_NAMES.get(statement.region_code, statement.region_code),
        "courier_type": statement.courier_type,
        "carrier_name": CARRIERS.get(statement.courier_type, statement.courier_type),
        "row_index": line.row_index, "waybill_no": line.waybill_no,
        "submitter_user_id": line.submitter_user_id,
        "submitter_name": line.submitter_name,
        "amount": line.amount, "raw_data": raw,
        "allocation_type": line.allocation_type,
        "dept_code": line.dept_code, "dept_name": line.dept_name,
        "project_code": line.project_code, "project_name": line.project_name,
        "match_source": line.match_source,
        "match_source_name": MATCH_SOURCE_LABELS.get(line.match_source, "待人工指定"),
        "issue": line.issue, "updated_by_name": creator_name,
        "updated_at": line.updated_at.isoformat(timespec="seconds") if line.updated_at else None,
    }


def assign_statement_line(
    db, line_id, allocation_type, code, *, dept_code=None, submitter_user_id=None,
    updated_by=None,
    output_dir=None,
):
    line = db.query(models.BillingStatementLine).filter(
        models.BillingStatementLine.id == line_id,
    ).first()
    if not line:
        raise ValueError("账单明细不存在")
    statement = db.query(models.BillingStatement).filter(
        models.BillingStatement.id == line.statement_id,
    ).first()
    override = {
        "allocation_type": allocation_type,
        "dept_code": code if allocation_type == "department" else dept_code,
        "project_code": code if allocation_type == "project" else None,
        "submitter_user_id": submitter_user_id or line.submitter_user_id,
    }
    result = resolve_waybill_allocation(
        db, line.waybill_no, statement.region_code, statement.courier_type,
        override=override,
    )
    if result.get("issue"):
        raise ValueError(result["issue"])
    siblings = db.query(models.BillingStatementLine).filter(
        models.BillingStatementLine.statement_id == statement.id,
        models.BillingStatementLine.waybill_no == line.waybill_no,
    ).all()
    for sibling in siblings:
        _apply_result(sibling, result, updated_by)
    db.commit()
    manifest = regenerate_statement_outputs(db, statement.id, output_dir=output_dir)
    return siblings, manifest


def _editable_raw_values(db, statement, raw_data, existing_line=None):
    """按账单原列顺序规范编辑值，并同步运单号与金额索引字段。"""
    raw_data = raw_data if isinstance(raw_data, dict) else {}
    headers = []
    if existing_line:
        try:
            headers = list(json.loads(existing_line.raw_data or "{}").keys())
        except (TypeError, ValueError):
            headers = []
    if not headers:
        headers = statement_raw_headers(db, statement)
    for header in raw_data:
        if header not in headers:
            headers.append(str(header))
    required_column = max(statement.waybill_column, statement.amount_column)
    if len(headers) < required_column:
        raise ValueError("账单列结构不完整，请重新上传原始账单")
    normalized = {header: raw_data.get(header, "") for header in headers}
    waybill_header = headers[statement.waybill_column - 1]
    amount_header = headers[statement.amount_column - 1]
    number = _waybill(normalized.get(waybill_header))
    if not number:
        raise ValueError("运单号不能为空")
    amount = float(_amount(normalized.get(amount_header)))
    normalized[waybill_header] = number
    normalized[amount_header] = amount
    return normalized, number, amount


def statement_raw_headers(db, statement, *, output_dir=None):
    """即使工作台明细被全部删除，仍从原始账单保留新增行所需的列结构。"""
    sample = db.query(models.BillingStatementLine).filter(
        models.BillingStatementLine.statement_id == statement.id,
    ).order_by(models.BillingStatementLine.row_index).first()
    if sample:
        try:
            headers = list(json.loads(sample.raw_data or "{}").keys())
        except (TypeError, ValueError):
            headers = []
        if headers:
            return headers
    source_path = _scope_dir(
        statement.month, statement.region_code, statement.courier_type, output_dir,
    ) / "原始账单.xlsx"
    if not source_path.is_file():
        return []
    workbook = load_workbook(source_path, read_only=True, data_only=False)
    try:
        if statement.detail_sheet not in workbook.sheetnames:
            return []
        sheet = workbook[statement.detail_sheet]
        return [
            str(sheet.cell(statement.header_row, column).value or f"列{column}")
            for column in range(1, sheet.max_column + 1)
        ]
    finally:
        workbook.close()


def update_statement_line_details(
    db, line, raw_data, allocation_type=None, dept_code=None, project_code=None,
    *, submitter_user_id=None, updated_by=None, output_dir=None,
):
    statement = db.query(models.BillingStatement).filter(
        models.BillingStatement.id == line.statement_id,
    ).first()
    if not statement:
        raise ValueError("费用账单不存在")
    normalized, number, amount = _editable_raw_values(
        db, statement, raw_data, existing_line=line,
    )
    line.raw_data = json.dumps(normalized, ensure_ascii=False)
    line.waybill_no = number
    line.amount = amount
    allocation_type = str(allocation_type or "").strip().lower()
    if allocation_type in {"department", "project"}:
        override = {
            "allocation_type": allocation_type,
            "dept_code": dept_code,
            "project_code": project_code if allocation_type == "project" else None,
            "submitter_user_id": submitter_user_id,
        }
        if allocation_type == "department":
            override["dept_code"] = dept_code
        result = resolve_waybill_allocation(
            db, number, statement.region_code, statement.courier_type,
            month=statement.month, override=override,
        )
        if result.get("issue"):
            raise ValueError(result["issue"])
    else:
        result = resolve_waybill_allocation(
            db, number, statement.region_code, statement.courier_type,
            month=statement.month,
        )
    _apply_result(line, result, updated_by)
    db.commit()
    return regenerate_statement_outputs(db, statement.id, output_dir=output_dir)


def create_statement_line(
    db, statement_id, raw_data, allocation_type=None, dept_code=None,
    project_code=None, *, submitter_user_id=None, updated_by=None, output_dir=None,
):
    statement = db.query(models.BillingStatement).filter(
        models.BillingStatement.id == statement_id,
    ).first()
    if not statement:
        raise ValueError("费用账单不存在")
    last_row = db.query(models.BillingStatementLine).filter(
        models.BillingStatementLine.statement_id == statement.id,
    ).order_by(models.BillingStatementLine.row_index.desc()).first()
    line = models.BillingStatementLine(
        statement_id=statement.id,
        row_index=(last_row.row_index + 1 if last_row else statement.header_row + 1),
        waybill_no="PENDING", amount=0, raw_data="{}",
    )
    db.add(line)
    db.flush()
    manifest = update_statement_line_details(
        db, line, raw_data, allocation_type, dept_code, project_code,
        submitter_user_id=submitter_user_id,
        updated_by=updated_by, output_dir=output_dir,
    )
    db.refresh(line)
    return line, statement, manifest


def delete_statement_line(db, line_id, *, output_dir=None):
    line = db.query(models.BillingStatementLine).filter(
        models.BillingStatementLine.id == line_id,
    ).first()
    if not line:
        raise ValueError("账单明细不存在")
    statement_id = line.statement_id
    db.delete(line)
    db.commit()
    return regenerate_statement_outputs(db, statement_id, output_dir=output_dir)


def rematch_statement(db, statement_id, *, output_dir=None):
    statement = db.query(models.BillingStatement).filter(
        models.BillingStatement.id == statement_id,
    ).first()
    if not statement:
        return None
    references = _reference_maps(db)
    submitters = _submitter_map(db)
    lines = db.query(models.BillingStatementLine).filter(
        models.BillingStatementLine.statement_id == statement.id,
    ).all()
    for line in lines:
        if line.match_source == "manual_assignment":
            continue
        result = resolve_waybill_allocation(
            db, line.waybill_no, statement.region_code, statement.courier_type,
            month=statement.month, references=references,
            submitters=submitters,
        )
        _apply_result(line, result)
    db.commit()
    return regenerate_statement_outputs(db, statement.id, output_dir=output_dir)


def rematch_for_manual_record(db, record, *, output_dir=None):
    statements = db.query(models.BillingStatement).filter(
        models.BillingStatement.region_code == record.region_code,
        models.BillingStatement.courier_type == record.courier_type,
    ).all()
    for statement in statements:
        exists = db.query(models.BillingStatementLine.id).filter(
            models.BillingStatementLine.statement_id == statement.id,
            models.BillingStatementLine.waybill_no == record.waybill_no,
        ).first()
        if exists:
            rematch_statement(db, statement.id, output_dir=output_dir)
