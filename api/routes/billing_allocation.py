"""Administrator workflow for importing, editing and exporting carrier bills."""
import json
import re
from typing import Any
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from config.settings import settings
from database.connection import get_db
from database import models
from utils.billing_allocation import (
    CARRIERS, OUTPUT_KINDS, REGION_NAMES, allocation_output_path,
    import_billing_statement, list_allocation_manifests, load_allocation_manifest,
)
from utils.billing_workbench import (
    MATCH_SOURCE_LABELS, assign_statement_line, create_statement_line,
    delete_statement_line, line_to_dict, regenerate_statement_outputs,
    rematch_statement,
    save_uploaded_statement, statement_raw_headers, update_statement_line_details,
)


router = APIRouter()


def _billing_scope_query(db, month, region, courier_type):
    if region != "all" and region not in REGION_NAMES:
        raise HTTPException(400, "区域筛选无效")
    courier_type = str(courier_type or "all").upper()
    if courier_type != "ALL" and courier_type not in CARRIERS:
        raise HTTPException(400, "快递筛选无效")
    if not month or len(month) != 7:
        raise HTTPException(400, "月份格式必须为 YYYY-MM")
    query = db.query(models.BillingStatement).filter(
        models.BillingStatement.month == month,
    )
    if region != "all":
        query = query.filter(models.BillingStatement.region_code == region)
    if courier_type != "ALL":
        query = query.filter(models.BillingStatement.courier_type == courier_type)
    return query, courier_type


def _month_range(start_month, end_month):
    pattern = r"\d{4}-(0[1-9]|1[0-2])"
    if not re.fullmatch(pattern, str(start_month or "")):
        raise HTTPException(400, "开始月份格式必须为 YYYY-MM")
    if not re.fullmatch(pattern, str(end_month or "")):
        raise HTTPException(400, "结束月份格式必须为 YYYY-MM")
    start_year, start_value = (int(part) for part in start_month.split("-"))
    end_year, end_value = (int(part) for part in end_month.split("-"))
    start_index = start_year * 12 + start_value - 1
    end_index = end_year * 12 + end_value - 1
    if start_index > end_index:
        raise HTTPException(400, "开始月份不能晚于结束月份")
    if end_index - start_index > 59:
        raise HTTPException(400, "一次最多查看 60 个月")
    return [f"{index // 12:04d}-{index % 12 + 1:02d}" for index in range(start_index, end_index + 1)]


@router.get("/billing-allocations/status")
def billing_allocation_status(
    month: str, region: str,
):
    try:
        manifests = list_allocation_manifests(month, region)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": True,
        "month": month,
        "region": region,
        "region_name": REGION_NAMES.get(region, ""),
        "items": manifests,
    }


@router.post("/billing-allocations/import")
async def import_monthly_carrier_bill(
    request: Request,
    month: str = Form(...),
    region: str = Form(...),
    courier_type: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            413, f"上传文件不能超过 {settings.max_upload_bytes // 1024 // 1024}MB"
        )
    try:
        # First validates the carrier workbook and stores the immutable original.
        import_billing_statement(
            db, data, file.filename or "账单.xlsx", month, region, courier_type,
        )
        current_user = getattr(request.state, "current_user", None) or {}
        manifest = save_uploaded_statement(
            db, data, file.filename or "账单.xlsx", month, region, courier_type,
            uploaded_by=current_user.get("id"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    return {"ok": True, "manifest": manifest}


@router.get("/billing-allocations/workbench")
def billing_allocation_workbench(
    month: str, region: str = "all", courier_type: str = "all",
    query: str = "", page: int = 1, page_size: int = 100,
    db: Session = Depends(get_db),
):
    statement_query, courier_type = _billing_scope_query(
        db, month, region, courier_type,
    )
    statements = statement_query.order_by(
        models.BillingStatement.region_code, models.BillingStatement.courier_type,
    ).all()
    statement_ids = [item.id for item in statements]
    line_query = db.query(models.BillingStatementLine).filter(
        models.BillingStatementLine.statement_id.in_(statement_ids or [-1]),
    )
    search = str(query or "").strip()
    if search:
        like = f"%{search}%"
        line_query = line_query.filter(or_(
            models.BillingStatementLine.waybill_no.like(like),
            models.BillingStatementLine.submitter_name.like(like),
            models.BillingStatementLine.dept_name.like(like),
            models.BillingStatementLine.project_name.like(like),
            models.BillingStatementLine.issue.like(like),
        ))
    total = line_query.count()
    page_size = min(max(int(page_size or 100), 10), 300)
    page = max(int(page or 1), 1)
    lines = line_query.order_by(
        models.BillingStatementLine.statement_id,
        models.BillingStatementLine.row_index,
    ).offset((page - 1) * page_size).limit(page_size).all()
    statement_map = {item.id: item for item in statements}
    statement_columns = {}
    raw_columns = []
    waybill_headers = []
    for statement in statements:
        sample = db.query(models.BillingStatementLine).filter(
            models.BillingStatementLine.statement_id == statement.id,
        ).order_by(models.BillingStatementLine.row_index).first()
        try:
            headers = (
                list(json.loads(sample.raw_data or "{}").keys())
                if sample else statement_raw_headers(db, statement)
            )
        except (TypeError, ValueError):
            headers = []
        waybill_header = (
            headers[statement.waybill_column - 1]
            if 0 < statement.waybill_column <= len(headers)
            else "运单号"
        )
        statement_columns[statement.id] = {
            "headers": headers, "waybill_header": waybill_header,
        }
        if waybill_header not in waybill_headers:
            waybill_headers.append(waybill_header)
        for header in headers:
            if header not in raw_columns:
                raw_columns.append(header)
    user_ids = {line.updated_by for line in lines if line.updated_by}
    names = {
        user.id: user.name for user in db.query(models.User).filter(
            models.User.id.in_(user_ids or [-1]),
        ).all()
    }
    manifests = []
    for statement in statements:
        manifest = load_allocation_manifest(
            statement.month, statement.region_code, statement.courier_type,
        ) or {}
        manifests.append({
            "statement_id": statement.id, "month": statement.month,
            "region_code": statement.region_code,
            "region_name": REGION_NAMES.get(statement.region_code, statement.region_code),
            "courier_type": statement.courier_type,
            "carrier_name": CARRIERS.get(statement.courier_type, statement.courier_type),
            "source_filename": statement.source_filename,
            "uploaded_at": statement.uploaded_at.isoformat(timespec="seconds"),
            "row_count": manifest.get("row_count", 0),
            "unmatched_rows": manifest.get("unmatched_rows", 0),
            "total_amount": manifest.get("total_amount", 0),
            "matched_amount": manifest.get("matched_amount", 0),
            "headers": statement_columns[statement.id]["headers"],
            "waybill_header": statement_columns[statement.id]["waybill_header"],
        })
    return {
        "ok": True, "month": month, "region": region,
        "courier_type": courier_type, "total": total, "page": page,
        "page_size": page_size, "statements": manifests,
        "raw_columns": raw_columns, "waybill_headers": waybill_headers,
        "items": [
            line_to_dict(line, statement_map[line.statement_id], names.get(line.updated_by))
            for line in lines
        ],
    }


@router.get("/billing-allocations/visualization")
def billing_allocation_visualization(
    start_month: str, end_month: str, region: str = "all",
    courier_type: str = "all",
    db: Session = Depends(get_db),
):
    months = _month_range(start_month, end_month)
    if region != "all" and region not in REGION_NAMES:
        raise HTTPException(400, "区域筛选无效")
    courier_type = str(courier_type or "all").upper()
    if courier_type != "ALL" and courier_type not in CARRIERS:
        raise HTTPException(400, "快递筛选无效")
    statement_query = db.query(models.BillingStatement).filter(
        models.BillingStatement.month >= start_month,
        models.BillingStatement.month <= end_month,
    )
    if region != "all":
        statement_query = statement_query.filter(
            models.BillingStatement.region_code == region,
        )
    if courier_type != "ALL":
        statement_query = statement_query.filter(
            models.BillingStatement.courier_type == courier_type,
        )
    statements = statement_query.all()
    statement_map = {item.id: item for item in statements}
    lines = db.query(models.BillingStatementLine).filter(
        models.BillingStatementLine.statement_id.in_(list(statement_map) or [-1]),
    ).all()
    start_year, start_value = (int(part) for part in start_month.split("-"))
    previous_index = start_year * 12 + start_value - 2
    previous_month = f"{previous_index // 12:04d}-{previous_index % 12 + 1:02d}"
    previous_query = db.query(models.BillingStatement).filter(
        models.BillingStatement.month == previous_month,
    )
    if region != "all":
        previous_query = previous_query.filter(
            models.BillingStatement.region_code == region,
        )
    if courier_type != "ALL":
        previous_query = previous_query.filter(
            models.BillingStatement.courier_type == courier_type,
        )
    previous_statements = previous_query.all()
    previous_map = {item.id: item for item in previous_statements}
    previous_lines = db.query(models.BillingStatementLine).filter(
        models.BillingStatementLine.statement_id.in_(list(previous_map) or [-1]),
    ).all()

    total_amount = sum(float(line.amount or 0) for line in lines)
    matched_lines = [line for line in lines if line.allocation_type in {"department", "project"}]
    matched_amount = sum(float(line.amount or 0) for line in matched_lines)
    waybills = {line.waybill_no for line in lines if line.waybill_no}

    def add(bucket, key, amount):
        if not key:
            return
        current = bucket.setdefault(key, {"name": key, "amount": 0.0, "rows": 0})
        current["amount"] += amount
        current["rows"] += 1

    allocation = {
        "department": {"name": "部门分摊", "amount": 0.0, "rows": 0},
        "project": {"name": "项目分摊", "amount": 0.0, "rows": 0},
    }
    departments, projects, sources, scopes, monthly = {}, {}, {}, {}, {}
    monthly_carriers = {}
    for line in lines:
        amount = float(line.amount or 0)
        if line.allocation_type in {"department", "project"}:
            allocation[line.allocation_type]["amount"] += amount
            allocation[line.allocation_type]["rows"] += 1
        if line.dept_name and line.allocation_type in {"department", "project"}:
            add(departments, line.dept_name, amount)
        if line.project_name and line.allocation_type == "project":
            add(projects, line.project_name, amount)
        if line.allocation_type in {"department", "project"}:
            source_name = MATCH_SOURCE_LABELS.get(line.match_source, "管理员指定")
            add(sources, source_name, amount)
        statement = statement_map[line.statement_id]
        add(monthly, statement.month, amount)
        carrier_month = monthly_carriers.setdefault(
            (statement.courier_type, statement.month),
            {"amount": 0.0, "waybills": set()},
        )
        carrier_month["amount"] += amount
        if line.waybill_no:
            carrier_month["waybills"].add(line.waybill_no)
        scope_name = (
            f"{REGION_NAMES.get(statement.region_code, statement.region_code)} · "
            f"{CARRIERS.get(statement.courier_type, statement.courier_type)}"
        )
        add(scopes, scope_name, amount)

    for line in previous_lines:
        statement = previous_map[line.statement_id]
        carrier_month = monthly_carriers.setdefault(
            (statement.courier_type, statement.month),
            {"amount": 0.0, "waybills": set()},
        )
        carrier_month["amount"] += float(line.amount or 0)
        if line.waybill_no:
            carrier_month["waybills"].add(line.waybill_no)

    def ranked(bucket, limit=10):
        return sorted(bucket.values(), key=lambda item: (-item["amount"], item["name"]))[:limit]

    trend_codes = list(CARRIERS) if courier_type == "ALL" else [courier_type]
    available_carrier_months = {
        (statement.courier_type, statement.month)
        for statement in statements + previous_statements
    }
    trend_series = []
    for code in trend_codes:
        points = []
        previous_bucket = monthly_carriers.get((code, previous_month), {"amount": 0.0})
        previous_amount = (
            round(previous_bucket["amount"], 2)
            if (code, previous_month) in available_carrier_months else None
        )
        for month in months:
            current = monthly_carriers.get((code, month), {"amount": 0.0, "waybills": set()})
            amount = round(current["amount"], 2)
            mom_change_pct = None
            current_available = (code, month) in available_carrier_months
            if current_available and previous_amount not in (None, 0):
                mom_change_pct = round((amount - previous_amount) / abs(previous_amount) * 100, 1)
            elif current_available and previous_amount == 0 and amount == 0:
                mom_change_pct = 0.0
            points.append({
                "month": month, "amount": amount,
                "waybill_count": len(current["waybills"]),
                "mom_change_pct": mom_change_pct,
            })
            previous_amount = amount if current_available else None
        trend_series.append({
            "code": code, "name": CARRIERS.get(code, code), "points": points,
        })

    return {
        "ok": True, "start_month": start_month, "end_month": end_month,
        "summary": {
            "total_amount": round(total_amount, 2),
            "matched_amount": round(matched_amount, 2),
            "unmatched_amount": round(total_amount - matched_amount, 2),
            "matched_rate": round((matched_amount / total_amount * 100) if total_amount else 0, 1),
            "waybill_count": len(waybills), "row_count": len(lines),
            "statement_count": len(statements),
        },
        "allocation": list(allocation.values()),
        "sources": ranked(sources, 8), "scopes": ranked(scopes, 12),
        "departments": ranked(departments), "projects": ranked(projects),
        "months": [monthly[month] for month in months if month in monthly],
        "trend_series": trend_series,
        "warnings": {
            "missing_months": [
                month for month in months
                if month not in {statement.month for statement in statements}
            ],
            "unmatched_rows": len(lines) - len(matched_lines),
            "unmatched_amount": round(total_amount - matched_amount, 2),
        },
    }


class LineAssignment(BaseModel):
    allocation_type: str
    code: str
    dept_code: str | None = None
    submitter_user_id: int | None = None


class LineDetails(BaseModel):
    raw_data: dict[str, Any]
    submitter_user_id: int | None = None
    allocation_type: str | None = None
    dept_code: str | None = None
    project_code: str | None = None


@router.patch("/billing-allocations/lines/{line_id}")
def update_billing_allocation_line(
    line_id: int, body: LineAssignment, request: Request,
    db: Session = Depends(get_db),
):
    allocation_type = str(body.allocation_type or "").lower()
    if allocation_type not in {"department", "project"}:
        raise HTTPException(400, "请选择部门分摊或项目分摊")
    current_user = getattr(request.state, "current_user", None) or {}
    try:
        lines, manifest = assign_statement_line(
            db, line_id, allocation_type, str(body.code or "").strip(),
            dept_code=str(body.dept_code or "").strip() or None,
            submitter_user_id=body.submitter_user_id,
            updated_by=current_user.get("id"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": True, "updated_rows": len(lines), "waybill_no": lines[0].waybill_no,
        "manifest": manifest,
    }


@router.patch("/billing-allocations/lines/{line_id}/details")
def update_billing_allocation_line_details(
    line_id: int, body: LineDetails, request: Request,
    db: Session = Depends(get_db),
):
    line = db.query(models.BillingStatementLine).filter(
        models.BillingStatementLine.id == line_id,
    ).first()
    if not line:
        raise HTTPException(404, "账单明细不存在")
    current_user = getattr(request.state, "current_user", None) or {}
    try:
        manifest = update_statement_line_details(
            db, line, body.raw_data, body.allocation_type,
            body.dept_code, body.project_code,
            submitter_user_id=body.submitter_user_id,
            updated_by=current_user.get("id"),
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    db.refresh(line)
    statement = db.query(models.BillingStatement).filter(
        models.BillingStatement.id == line.statement_id,
    ).first()
    return {
        "ok": True, "item": line_to_dict(line, statement), "manifest": manifest,
    }


@router.post("/billing-allocations/statements/{statement_id}/lines")
def add_billing_allocation_line(
    statement_id: int, body: LineDetails, request: Request,
    db: Session = Depends(get_db),
):
    current_user = getattr(request.state, "current_user", None) or {}
    try:
        line, statement, manifest = create_statement_line(
            db, statement_id, body.raw_data, body.allocation_type,
            body.dept_code, body.project_code,
            submitter_user_id=body.submitter_user_id,
            updated_by=current_user.get("id"),
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": True, "item": line_to_dict(line, statement), "manifest": manifest,
    }


@router.delete("/billing-allocations/lines/{line_id}")
def remove_billing_allocation_line(
    line_id: int, db: Session = Depends(get_db),
):
    try:
        manifest = delete_statement_line(db, line_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "manifest": manifest}


@router.post("/billing-allocations/{statement_id}/rematch")
def rematch_billing_statement(statement_id: int, db: Session = Depends(get_db)):
    try:
        manifest = rematch_statement(db, statement_id)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if not manifest:
        raise HTTPException(404, "费用账单不存在")
    return {"ok": True, "manifest": manifest}


@router.get("/billing-allocations/{month}/{region}/{courier_type}/{kind}")
def download_billing_allocation(
    month: str, region: str, courier_type: str, kind: str,
    db: Session = Depends(get_db),
):
    statement = db.query(models.BillingStatement).filter(
        models.BillingStatement.month == month,
        models.BillingStatement.region_code == region,
        models.BillingStatement.courier_type == str(courier_type).upper(),
    ).first()
    if statement:
        try:
            # Also upgrades historical detail exports to the current column
            # layout immediately before download.
            regenerate_statement_outputs(db, statement.id)
        except RuntimeError:
            # Preserve the existing clear 404/file fallback below when an old
            # installation no longer has its immutable source workbook.
            pass
    path, filename = allocation_output_path(month, region, courier_type, kind)
    if not path:
        carrier = CARRIERS.get(str(courier_type).upper(), "该快递")
        label = OUTPUT_KINDS.get(kind, "文件")
        raise HTTPException(404, f"{carrier}{label}尚未生成，请先上传对应月度账单")
    return FileResponse(
        path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
