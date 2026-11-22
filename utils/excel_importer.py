"""
Excel 批量导入工具
支持 .xlsx / .xls / .csv
模板下载 + 项目/部门批量导入解析
"""
import io
import csv
from typing import List, Dict, Tuple

COL_ALIASES_PROJECT = {
    "项目编号": ["项目编号", "项目代码", "编号", "编号code", "project_code", "code"],
    "项目名称": ["项目名称", "项目名称*", "项目", "名称", "project_name", "name"],
    "项目状态": ["项目状态", "状态", "启用状态", "enabled", "status"],
    "执行部门编号": ["执行部门编号", "部门编号", "所属部门编号", "dept_code"],
    "执行部门名称": ["执行部门名称", "部门名称", "所属部门", "dept_name"],
}

COL_ALIASES_DEPT = {
    "部门编号": ["部门编号", "部门代码", "编号", "dept_code", "code"],
    "部门名称": ["部门名称", "部门", "名称", "dept_name", "name"],
}


def _normalize_header(header: str) -> str:
    h = str(header or "").strip().replace(" ", "").replace("\u3000", "")
    return h.lower()


def _match_column(header_row, alias_map: dict) -> dict:
    """把 Excel 表头 → 标准字段名"""
    matched = {}
    for std_name, aliases in alias_map.items():
        for i, h in enumerate(header_row):
            if _normalize_header(h) in [_normalize_header(a) for a in aliases]:
                matched[std_name] = i
                break
    return matched


def _read_all_rows(file_storage) -> List[List]:
    """统一读取 Excel / CSV 的全部原始行。"""
    from config.settings import settings
    filename = (getattr(file_storage, "filename", "") or "").lower()
    reader = file_storage.file if hasattr(file_storage, "file") else file_storage
    content = reader.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise ValueError(f"上传文件不能超过 {settings.max_upload_bytes // 1024 // 1024}MB")
    if filename.endswith(".csv") or filename.startswith("csv"):
        text = content.decode("utf-8-sig", errors="replace")
        return list(csv.reader(io.StringIO(text)))
    try:
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        ws = wb.active
        return [[str(c.value).strip() if c.value is not None else "" for c in row]
                for row in ws.iter_rows()]
    except Exception:
        try:
            import pandas as pd
            df = pd.read_excel(io.BytesIO(content), header=None)
            return df.fillna("").astype(str).values.tolist()
        except Exception as e:
            raise ValueError(f"无法解析文件: {e}")


def _read_file(file_storage) -> Tuple[List[str], List[List]]:
    """读取首行为表头的普通 Excel / CSV。"""
    rows = _read_all_rows(file_storage)
    if not rows:
        return [], []
    return rows[0], rows[1:]


def parse_projects_excel(file_storage) -> Tuple[List[Dict], List[str]]:
    """
    解析项目 Excel。兼容两种格式：
    1. 项目编号 | 项目名称 | 执行部门编号 | 执行部门名称
    2. 项目名称* | 项目状态（项目名称可用 / 或两侧带空格的 - 表示最多 5 级层级）
    """
    all_rows = _read_all_rows(file_storage)
    header_index = None
    col_map = {}
    for index, row in enumerate(all_rows):
        matched = _match_column(row, COL_ALIASES_PROJECT)
        if "项目名称" in matched:
            header_index = index
            col_map = matched
            break
    if header_index is None:
        raise ValueError("未找到“项目名称”列")

    errors = []
    result = []
    for row_idx, row in enumerate(all_rows[header_index + 1:], start=header_index + 2):
        if not any(str(c).strip() for c in row):
            continue

        def g(std):
            idx = col_map.get(std)
            return str(row[idx]).strip() if idx is not None and idx < len(row) else ""

        name_or_path = g("项目名称")
        if not name_or_path:
            errors.append(f"第{row_idx}行: 项目名称为空")
            continue
        if _normalize_header(name_or_path) in {
            _normalize_header(alias) for alias in COL_ALIASES_PROJECT["项目名称"]
        }:
            continue
        status = g("项目状态")
        enabled = status not in ("停用", "禁用", "否", "0", "false", "False")
        result.append({
            "project_code": g("项目编号"),
            "project_name": name_or_path,
            "project_path": name_or_path,
            "dept_code": g("执行部门编号"),
            "dept_name": g("执行部门名称"),
            "enabled": enabled,
        })
    return result, errors


def parse_departments_excel(file_storage) -> Tuple[List[Dict], List[str]]:
    """
    解析部门 Excel → (有效记录列表, 错误信息列表)
    Excel 列: 部门编号 | 部门名称
    """
    header_row, data_rows = _read_file(file_storage)
    col_map = _match_column(header_row, COL_ALIASES_DEPT)
    errors = []
    required = ["部门编号", "部门名称"]
    missing = [r for r in required if r not in col_map]
    if missing:
        raise ValueError(f"缺少必填列: {', '.join(missing)}。当前表头: {header_row}")
    result = []
    for row_idx, row in enumerate(data_rows, start=2):
        if not any(str(c).strip() for c in row):
            continue
        def g(std):
            idx = col_map.get(std)
            return str(row[idx]).strip() if idx is not None and idx < len(row) else ""
        code = g("部门编号")
        name = g("部门名称")
        if not code or not name:
            errors.append(f"第{row_idx}行: 部门编号或名称为空")
            continue
        result.append({"dept_code": code, "dept_name": name})
    return result, errors


def generate_template(file_type: str = "projects") -> bytes:
    """生成空白 Excel 模板 bytes (带表头 + 示例行)"""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    if file_type == "projects":
        ws.title = "项目列表"
        headers = ["项目编号", "项目名称", "执行部门编号", "执行部门名称"]
        ws.append(headers)
        ws.append(["P2025001", "XX平台研发", "D005", "研发部"])
        ws.append(["P2025002", "年度审计", "D003", "财务部"])
    else:
        ws.title = "部门列表"
        headers = ["部门编号", "部门名称"]
        ws.append(headers)
        ws.append(["D001", "总经理办公室"])
        ws.append(["D005", "研发部"])
    # 简单列宽
    for i, h in enumerate(headers, 1):
        ws.column_dimensions[chr(64 + i) if i <= 26 else "A"].width = 20
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
