"""
批量寄件路由 — Batch workflow

状态机: draft → pending_approval → approved → ordering → done/partial/failed
"""
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from database.connection import get_db
from database import models
from utils.order_status import order_status_display
from utils.approval_policy import (
    actor_id_from_request, approve_level, assert_assigned_approver,
    assign_approval, ensure_approval_snapshot, reject_level,
)
from utils.sender_addresses import assert_sender_address_allowed
from utils.approval_notifications import (
    queue_approval_notification, queue_approval_progress_notification,
    queue_approval_result_notification, queue_dispatch_notification,
)
import datetime as dt, uuid, io, re, json

router = APIRouter()
BATCH_MAX_RECIPIENTS = 100

BATCH_FIELD_MAP = {
    "courier_type": "courier_type", "express_type": "express_type",
    "express_type_name": "express_type_name", "sender_name": "sender_name",
    "sender_phone": "sender_phone", "sender_company": "sender_company",
    "sender_province": "sender_province", "sender_city": "sender_city",
    "sender_district": "sender_district", "sender_address": "sender_address",
    "item_name": "item_name", "item_weight": "item_weight",
    "item_volume": "item_volume", "insurance_amount": "insurance",
    "parcel_count": "parcel_count", "item_count": "item_count_per_pkg",
    "item_remark": "item_remark", "shipping_reason": "shipping_reason",
    "pickup_time_mode": "pickup_time_mode", "estimated_freight": "estimated_freight",
    "cost_type": "cost_type", "dept_code": "dept_code", "dept_name": "dept_name",
    "project_code": "project_code", "project_name": "project_name",
}


def _apply_batch_fields(batch, shared: dict) -> None:
    for source, target in BATCH_FIELD_MAP.items():
        if source in shared:
            setattr(batch, target, shared[source] if shared[source] != "" else None)
    for source, target in (
        ("pickup_start_time", "pickup_start_time"),
        ("pickup_end_time", "pickup_end_time"),
    ):
        if source not in shared:
            continue
        value = shared.get(source)
        setattr(batch, target, dt.datetime.fromisoformat(value) if value else None)
    batch.courier_type = str(batch.courier_type or "").upper()


def _sync_order_from_batch(order, batch) -> None:
    for field in (
        "courier_type", "region_code", "billing_account_code", "express_type",
        "express_type_name", "sender_name", "sender_phone", "sender_company",
        "sender_province", "sender_city", "sender_district", "sender_address",
        "item_name", "item_weight", "item_volume", "insurance", "parcel_count",
        "item_remark", "shipping_reason", "pickup_time_mode", "pickup_start_time",
        "pickup_end_time", "cost_type", "dept_code", "dept_name", "project_code",
        "project_name",
    ):
        setattr(order, field, getattr(batch, field))
    order.item_count = batch.item_count_per_pkg


def _request_auth_enabled(request: Request) -> bool:
    return bool(getattr(request.state, "auth_enabled", False))


def _current_user_id(request: Request) -> Optional[int]:
    current_user = getattr(request.state, "current_user", None) or {}
    return int(current_user["id"]) if current_user.get("id") is not None else None


def _is_admin_request(request: Request) -> bool:
    current_user = getattr(request.state, "current_user", None) or {}
    return str(current_user.get("role") or "employee") == "admin"


def _preview_admin_requested(request: Request) -> bool:
    headers = getattr(request, "headers", {}) or {}
    return (
        not bool(getattr(request.state, "auth_enabled", False))
        and str(headers.get("X-Preview-Role") or "").lower() == "admin"
    )


def _assert_admin_request(request: Request, db: Session):
    current_user = getattr(request.state, "current_user", None) or {}
    if str(current_user.get("role") or "") == "admin":
        return current_user
    if _preview_admin_requested(request):
        preview_admin = db.query(models.User).filter(
            models.User.role == "admin", models.User.is_active.is_(True)
        ).order_by(models.User.id.asc()).first()
        if preview_admin:
            return {"id": preview_admin.id, "name": "Preview 管理员", "role": "admin"}
        raise HTTPException(400, "Preview 数据中没有可用于演示的管理员账号")
    raise HTTPException(403, "仅管理员可以跳过审批层级")


def _skip_remark(level: int, admin_name: str, remark: str = "") -> str:
    prefix = f"[管理员跳过] {admin_name}跳过第{level}级审批"
    return f"{prefix}：{remark.strip()}" if str(remark or "").strip() else prefix


def _assert_batch_owner(batch, request: Request) -> None:
    if not _request_auth_enabled(request) or _is_admin_request(request):
        return
    if _current_user_id(request) != getattr(batch, "created_by", None):
        raise HTTPException(403, "无权操作该批次")


def _assert_batch_readable(batch, request: Request) -> None:
    if not _request_auth_enabled(request) or _is_admin_request(request):
        return
    user_id = _current_user_id(request)
    allowed_ids = {
        getattr(batch, "created_by", None),
        getattr(batch, "approver_id", None),
        getattr(batch, "approval_level1_user_id", None),
        getattr(batch, "approval_level2_user_id", None),
    }
    if user_id not in allowed_ids:
        raise HTTPException(403, "无权查看该批次")


class BatchDraftCreate(BaseModel):
    courier_type: str
    express_type: Optional[str] = None
    express_type_name: Optional[str] = None
    sender_name: str; sender_phone: str
    sender_company: Optional[str] = None
    sender_province: Optional[str] = None
    sender_city: Optional[str] = None
    sender_district: Optional[str] = None
    sender_address: Optional[str] = None
    item_name: str; item_weight: Optional[float] = None
    item_volume: Optional[float] = None
    insurance_amount: float = 0
    parcel_count: int = 1
    item_count_per_pkg: int = 1
    item_remark: Optional[str] = None
    shipping_reason: Optional[str] = None
    pickup_time_mode: str = "immediate"
    pickup_start_time: Optional[str] = None
    pickup_end_time: Optional[str] = None
    estimated_freight: Optional[float] = None
    cost_type: Optional[str] = None
    dept_code: Optional[str] = None; dept_name: Optional[str] = None
    project_code: Optional[str] = None; project_name: Optional[str] = None
    created_by: Optional[int] = None; remark: Optional[str] = None
    debug_region_code: Optional[str] = None


def _batch_no():
    return "BA" + dt.datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:4].upper()


def _to_dict(b, db=None):
    d = {c.name: getattr(b, c.name) for c in models.Batch.__table__.columns}
    # details 存的是 JSON 字符串 → 解析成 list
    import json as _json
    if d.get("details"):
        try: d["details"] = _json.loads(d["details"])
        except: pass
    for k in (
        "created_at", "approve_time", "updated_at", "pickup_start_time", "pickup_end_time",
        "approval_level1_time", "approval_level2_time",
    ):
        if d.get(k): d[k] = str(d[k])
    if db:
        d["quote_total"] = round(sum(
            float(value or 0) for (value,) in db.query(models.Order.freight).filter(
                models.Order.batch_id == b.id
            ).all()
        ), 2)
        approval_user_ids = {
            value for value in (
                b.approval_level1_user_id, b.approval_level2_user_id, b.approver_id
            ) if value
        }
        approval_users = db.query(models.User).filter(
            models.User.id.in_(approval_user_ids)
        ).all() if approval_user_ids else []
        approval_names = {user.id: user.name or "未命名员工" for user in approval_users}
        d["approval_level1_user_name"] = approval_names.get(b.approval_level1_user_id)
        d["approval_level2_user_name"] = approval_names.get(b.approval_level2_user_id)
        d["orders"] = []
        for o in db.query(models.Order).filter(models.Order.batch_id == b.id).all():
            display = order_status_display(o.status, o.track_status, bool(o.courier_order_no))
            d["orders"].append({
                "id": o.id, "order_no": o.order_no, "receiver_name": o.receiver_name,
                "receiver_phone": o.receiver_phone, "receiver_company": o.receiver_company,
                "receiver_province": o.receiver_province, "receiver_city": o.receiver_city,
                "receiver_district": o.receiver_district, "receiver_address": o.receiver_address,
                "status": o.status, "status_category": display["status_category"],
                "status_label": display["status_label"],
                "courier_order_no": o.courier_order_no,
                "shipping_reason": o.shipping_reason, "item_name": o.item_name,
                "freight": o.freight, "track_status": o.track_status,
                "estimated_delivery_time": (
                    o.estimated_delivery_time.isoformat() if o.estimated_delivery_time else None
                ),
                "order_response": o.order_response,
                "created_at": str(o.created_at) if o.created_at else None,
                "order_time": str(o.order_time) if o.order_time else None,
            })
        d["validation_passed"] = sum(o["status"] != "validation_failed" for o in d["orders"])
        d["validation_failed"] = sum(o["status"] == "validation_failed" for o in d["orders"])
    return d


def _valid_phone(p):
    return bool(str(p or "").strip())


def _parse_excel_bytes(data):
    from openpyxl import load_workbook
    alias = {
        "receiver_name": ["收件人姓名", "姓名", "收货人", "收件人", "receiver_name", "name"],
        "receiver_phone": ["收件人电话", "手机号", "联系电话", "电话", "手机", "phone", "mobile"],
        "receiver_full_address": ["收件人地址", "收货地址", "完整地址", "full_address"],
        "receiver_province": ["省", "省份", "province"],
        "receiver_city": ["市", "城市", "city"],
        "receiver_district": ["区", "区县", "区/县", "区(选填)", "区域", "district"],
        "receiver_address": ["详细地址", "地址", "收件地址", "detail", "address"],
        "receiver_company": ["公司", "单位", "receiver_company", "company"],
    }
    wb = load_workbook(io.BytesIO(data), data_only=True)
    ws = wb.active
    header = [str(c.value or "").strip() for c in ws[1]]
    header_map = {}
    for idx, col in enumerate(header):
        cl = col.lower()
        for f, aliases in alias.items():
            if cl in [a.lower() for a in aliases]: header_map[f] = idx; break
    required = ["receiver_name", "receiver_phone"]
    if "receiver_full_address" not in header_map:
        required.extend(["receiver_province", "receiver_city", "receiver_district", "receiver_address"])
    missing = [r for r in required if r not in header_map]
    if missing: raise ValueError(f"Excel 表头缺少必填列: {missing}. 实际表头: {header}")

    rows, errors = [], []
    for row_idx in range(2, ws.max_row + 1):
        cells = [c.value for c in ws[row_idx]]
        if not any(c not in (None, "") for c in cells): continue
        def g(f):
            v = cells[header_map[f]] if f in header_map else None
            return (str(v).strip() if v is not None else "")
        name, phone = g("receiver_name"), g("receiver_phone")
        full_address = g("receiver_full_address")
        province, city, district, address = g("receiver_province"), g("receiver_city"), g("receiver_district"), g("receiver_address")
        errs = []
        if not name: errs.append("姓名必填")
        if not _valid_phone(phone): errs.append("联系电话必填")
        if "receiver_full_address" in header_map:
            from utils.address_parser import parse_address
            parsed = parse_address(full_address)
            if parsed.get("success"):
                province, city, district, address = (
                    parsed["province"], parsed["city"], parsed["district"], parsed["address"]
                )
            else:
                errs.append(f"地址识别失败：{parsed.get('message') or '请补充完整省市区'}")
        else:
            if not province: errs.append("省份必填")
            if not city: errs.append("城市必填")
            if not district: errs.append("区县必填")
            if not address or len(address) < 3: errs.append("详细地址过短(≥3字)")
            if province and city and district:
                from api.address_validation import validate_division
                division_error = validate_division(province, city, district)
                if division_error: errs.append(f"省市区无效：{division_error}")
        if errs:
            errors.append({"row": row_idx, "errors": errs, "preview": {
                "name": name, "phone": phone, "address": full_address or "".join([province, city, district, address]),
            }})
        else:
            rows.append({
                "receiver_name": name, "receiver_phone": phone,
                "receiver_province": province, "receiver_city": city,
                "receiver_district": district,
                "receiver_address": address, "receiver_company": g("receiver_company"),
                "receiver_full_address": full_address or "".join([province, city, district, address]),
            })
    return rows, errors


# ====== 静态路径必须在 /{batch_id} 之前 ======

@router.get("/template")
def download_template():
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active; ws.title = "收件人"
    ws.append(["收件人姓名", "收件人电话", "收件人地址"])
    for s in [["演示收件人甲","13800000000","广东省深圳市南山区示例路1号"],
              ["演示收件人乙","13900000001","北京市朝阳区示例路2号"],
              ["演示收件人丙","13700000002","福建省厦门市思明区示例路3号"]]:
        ws.append(s)
    for col, w in zip("ABC", [16, 16, 56]):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:C{ws.max_row}"
    help_ws = wb.create_sheet("填写说明")
    instructions = [
        ["批量下单模板填写说明"],
        ["1", "请保留第一行表头，按行填写，每行对应一个独立快递订单。"],
        ["2", "姓名、电话、收件人地址为必填；地址单元格只填写地址，不要依赖其中的姓名和电话。"],
        ["3", "省份可以省略，城市可以省略“市”字；系统会按标准行政区划补全并拆分省、市、区县和详细地址。"],
        ["4", "若区县重名且地址不足以唯一定位，系统会提示补充城市，不会自行猜测。"],
        ["5", f"单次最多 {BATCH_MAX_RECIPIENTS} 个收件人；文件中任一行有误时，整批不能提交审核。"],
        ["6", "系统会在提交审核前逐条预下单校验，审批通过后再逐条正式下单。"],
    ]
    for row in instructions: help_ws.append(row)
    help_ws.column_dimensions["A"].width = 8
    help_ws.column_dimensions["B"].width = 90
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return StreamingResponse(buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="batch_recipients_template.xlsx"'})


@router.post("/upload-draft")
def create_upload_draft(body: dict, request: Request, db: Session = Depends(get_db)):
    """Create an import-only shell without requiring the rest of the order form."""
    current_user = getattr(request.state, "current_user", None)
    from utils.regions import request_region
    region_code = request_region(
        db, current_user, body.get("debug_region_code"),
        auth_enabled=_request_auth_enabled(request),
    )
    batch = models.Batch(
        batch_no=_batch_no(), region_code=region_code,
        courier_type="PENDING", sender_name="待填写", sender_phone="待填写",
        item_name="待填写", parcel_count=1, item_count_per_pkg=1,
        pickup_time_mode="immediate", status="draft",
        created_by=(current_user or {}).get("id"),
    )
    db.add(batch); db.commit(); db.refresh(batch)
    return {"ok": True, "batch_id": batch.id, "batch_no": batch.batch_no}


@router.post("/draft")
def create_draft(draft: BatchDraftCreate, request: Request, db: Session = Depends(get_db)):
    current_user = getattr(request.state, "current_user", None)
    from utils.regions import enabled_carriers, get_config, request_region
    region_code = request_region(
        db, current_user, draft.debug_region_code,
        auth_enabled=_request_auth_enabled(request),
    )
    if current_user:
        draft.created_by = current_user["id"]
    if not _valid_phone(draft.sender_phone):
        raise HTTPException(400, "寄件人联系电话不能为空")
    assert_sender_address_allowed(db, current_user, draft.model_dump())
    if draft.cost_type:
        normalized_cost_type = str(draft.cost_type).strip().lower()
        if normalized_cost_type in {"部门", "department"}:
            draft.cost_type = "department"
            draft.project_code = None
            draft.project_name = None
        elif normalized_cost_type in {"项目", "project"}:
            draft.cost_type = "project"
            if not str(draft.project_name or "").strip():
                raise HTTPException(400, "选择项目归属时必须上传项目")
        else:
            raise HTTPException(400, "费用归属只能是部门或项目")
        if not str(draft.dept_name or "").strip():
            raise HTTPException(400, "必须上传费用归属部门")
    if draft.parcel_count < 1 or draft.parcel_count > 307:
        raise HTTPException(400, "包裹数量必须是1至307之间的整数")
    if draft.parcel_count > 1 and draft.courier_type.upper() not in {"SF", "JDL"}:
        raise HTTPException(400, "多包裹下单目前只支持顺丰和京东")
    if draft.courier_type.upper() == "JDL" and draft.insurance_amount > 300000:
        raise HTTPException(400, "京东快递普通保价声明价值不能超过300000元")
    if draft.courier_type.upper() not in enabled_carriers(db, region_code):
        raise HTTPException(400, "当前工作区域未启用该快递服务")
    billing_key = {"SF": "sf_monthly_card", "DB": "db_customer_code", "JDL": "jdl_customer_code"}.get(draft.courier_type.upper())
    b = models.Batch(
        batch_no=_batch_no(), region_code=region_code,
        billing_account_code=get_config(db, region_code, billing_key, "") if billing_key else None,
        courier_type=draft.courier_type.upper(),
        express_type=draft.express_type, express_type_name=draft.express_type_name,
        sender_name=draft.sender_name, sender_phone=draft.sender_phone,
        sender_company=draft.sender_company,
        sender_province=draft.sender_province, sender_city=draft.sender_city,
        sender_district=draft.sender_district,
        sender_address=draft.sender_address,
        item_name=draft.item_name, item_weight=draft.item_weight,
        item_volume=draft.item_volume, insurance=draft.insurance_amount,
        parcel_count=draft.parcel_count, item_count_per_pkg=draft.item_count_per_pkg or 1,
        item_remark=draft.item_remark, shipping_reason=draft.shipping_reason,
        pickup_time_mode=draft.pickup_time_mode,
        pickup_start_time=dt.datetime.fromisoformat(draft.pickup_start_time) if draft.pickup_start_time else None,
        pickup_end_time=dt.datetime.fromisoformat(draft.pickup_end_time) if draft.pickup_end_time else None,
        estimated_freight=draft.estimated_freight,
        cost_type=draft.cost_type, dept_code=draft.dept_code, dept_name=draft.dept_name,
        project_code=draft.project_code, project_name=draft.project_name,
        status="draft", created_by=draft.created_by, remark=draft.remark,
    )
    db.add(b); db.commit(); db.refresh(b)
    return {"ok": True, "batch_id": b.id, "batch_no": b.batch_no}


# ====== 带 /{batch_id} 的路径 ======

@router.post("/{batch_id}/import")
async def import_excel(batch_id: int, request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    batch = db.query(models.Batch).filter(models.Batch.id == batch_id).first()
    if not batch: raise HTTPException(404, "批次不存在")
    _assert_batch_owner(batch, request)
    if batch.status != "draft": raise HTTPException(400, f"只有 draft 可导入, 当前: {batch.status}")
    if not (file.filename or "").lower().endswith(".xlsx"): raise HTTPException(400, "只支持 .xlsx 文件")
    from config.settings import settings
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(413, f"上传文件不能超过 {settings.max_upload_bytes // 1024 // 1024}MB")
    try: rows, errors = _parse_excel_bytes(data)
    except ValueError as e: raise HTTPException(400, str(e))
    total_rows = len(rows) + len(errors)
    if total_rows > BATCH_MAX_RECIPIENTS:
        raise HTTPException(400, f"单次最多 {BATCH_MAX_RECIPIENTS} 人，当前文件有 {total_rows} 行")
    import json as _json
    batch.total_count = total_rows
    batch.details = _json.dumps(rows, ensure_ascii=False) if not errors else None
    db.commit()
    return {"ok": True, "total": total_rows, "ok_count": len(rows),
            "error_count": len(errors), "can_commit": bool(rows) and not errors,
            "max_warnings": errors[:20], "sample": rows[:5], "limit": BATCH_MAX_RECIPIENTS}


@router.post("/{batch_id}/commit")
def commit_batch(batch_id: int, body: dict, request: Request, db: Session = Depends(get_db)):
    batch = db.query(models.Batch).filter(models.Batch.id == batch_id).first()
    if not batch: raise HTTPException(404, "批次不存在")
    _assert_batch_owner(batch, request)
    if batch.status != "draft": raise HTTPException(400, f"只有 draft 可提交, 当前: {batch.status}")
    shared = body.get("batch_fields") or {}
    _apply_batch_fields(batch, shared)
    from utils.regions import enabled_carriers, get_config
    if batch.courier_type not in enabled_carriers(db, batch.region_code):
        raise HTTPException(400, "当前工作区域未启用所选快递服务")
    billing_key = {
        "SF": "sf_monthly_card", "DB": "db_customer_code", "JDL": "jdl_customer_code",
    }.get(batch.courier_type)
    batch.billing_account_code = (
        get_config(db, batch.region_code, billing_key, "") if billing_key else None
    )
    if not _valid_phone(batch.sender_phone):
        raise HTTPException(400, "寄件人联系电话不能为空")
    assert_sender_address_allowed(db, getattr(request.state, "current_user", None), {
        "sender_company": batch.sender_company, "sender_province": batch.sender_province,
        "sender_city": batch.sender_city, "sender_district": batch.sender_district,
        "sender_address": batch.sender_address,
    })
    if int(batch.parcel_count or 1) > 1 and batch.courier_type not in {"SF", "JDL"}:
        raise HTTPException(400, "多包裹下单目前只支持顺丰和京东")
    if batch.courier_type == "JDL" and float(batch.insurance or 0) > 300000:
        raise HTTPException(400, "京东快递普通保价声明价值不能超过300000元")
    import json as _json
    rows = body.get("rows")
    if not rows and batch.details:
        try: rows = _json.loads(batch.details)
        except: rows = []
    rows = rows or []
    if not rows: raise HTTPException(400, "请先导入收件人信息")
    if len(rows) > BATCH_MAX_RECIPIENTS:
        raise HTTPException(400, f"单次最多 {BATCH_MAX_RECIPIENTS} 人")
    if db.query(models.Order).filter(models.Order.batch_id == batch.id).count():
        raise HTTPException(400, "该批次已生成明细订单，请勿重复提交")
    from courier import get_courier
    from api.order_dispatch import build_order_data
    courier = get_courier(batch.courier_type, db, batch.region_code)
    committed = passed = failed = 0
    validation_errors = []
    ticket_quotes = []
    try:
        for row in rows:
            order_no = "CO" + dt.datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:6].upper()
            while db.query(models.Order).filter(models.Order.order_no == order_no).count() > 0:
                order_no = "CO" + dt.datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:6].upper()
            o = models.Order(
                order_no=order_no, courier_type=batch.courier_type,
                region_code=batch.region_code, billing_account_code=batch.billing_account_code,
                express_type=batch.express_type, express_type_name=batch.express_type_name,
                sender_name=batch.sender_name, sender_phone=batch.sender_phone,
                sender_company=batch.sender_company,
                sender_province=batch.sender_province, sender_city=batch.sender_city,
                sender_district=batch.sender_district,
                sender_address=batch.sender_address,
                receiver_name=row["receiver_name"], receiver_phone=row["receiver_phone"],
                receiver_company=row.get("receiver_company"),
                receiver_province=row["receiver_province"], receiver_city=row["receiver_city"],
                receiver_district=row.get("receiver_district"), receiver_address=row["receiver_address"],
                item_name=batch.item_name, item_weight=batch.item_weight, item_volume=batch.item_volume,
                insurance=batch.insurance, parcel_count=batch.parcel_count,
                freight=batch.estimated_freight,
                item_count=batch.item_count_per_pkg,
                item_remark=batch.item_remark, shipping_reason=batch.shipping_reason,
                pickup_time_mode=batch.pickup_time_mode or "immediate",
                pickup_start_time=batch.pickup_start_time, pickup_end_time=batch.pickup_end_time,
                cost_type=batch.cost_type, dept_code=batch.dept_code, dept_name=batch.dept_name,
                project_code=batch.project_code, project_name=batch.project_name,
                batch_id=batch.id, created_by=batch.created_by,
                status="prechecking", source="batch", source_id=batch.batch_no,
            )
            db.add(o); db.flush(); committed += 1
            from courier_services import ALL_SERVICES, available_in_region
            selected_service = next((service for service in ALL_SERVICES
                if service["courier"] == batch.courier_type
                and service["express_type"] == str(batch.express_type or "")
                and available_in_region(service, batch.region_code)), None)
            if not selected_service:
                o.status = "validation_failed"
                failed += 1
                validation_errors.append({
                    "order_id": o.id, "receiver_name": o.receiver_name,
                    "receiver_phone": o.receiver_phone,
                    "message": "所选快递产品与承运商不匹配",
                })
                continue
            # SF/DB 使用实时报价接口；JDL 的费用与时效由前置校验返回。
            # 测试替身可以不提供报价方法，此时保留已有预估值。
            if (
                batch.courier_type == "JDL"
                or callable(getattr(courier, "query_delivery_quotes", None))
            ):
                from api.routes.courier import QuoteQuery, require_selected_service_quote
                try:
                    selected_quote = require_selected_service_quote(
                        selected_service["id"],
                        QuoteQuery(
                            sender_province=o.sender_province, sender_city=o.sender_city,
                            sender_district=o.sender_district, sender_address=o.sender_address,
                            receiver_province=o.receiver_province, receiver_city=o.receiver_city,
                            receiver_district=o.receiver_district, receiver_address=o.receiver_address,
                            item_type=o.item_name, weight=o.item_weight, volume=o.item_volume,
                            parcel_count=o.parcel_count, insurance_amount=o.insurance or 0,
                            pickup_start_time=(o.pickup_start_time.isoformat() if o.pickup_start_time else None),
                            same_day_required=False,
                        ), db, batch.region_code, prefer_live=True,
                    )
                    o.freight = selected_quote.get("estimated_fee")
                    from api.order_dispatch import parse_delivery
                    o.estimated_delivery_time = parse_delivery(
                        selected_quote.get("estimated_delivery")
                    )
                except HTTPException as exc:
                    o.status = "validation_failed"
                    failed += 1
                    validation_errors.append({
                        "order_id": o.id, "receiver_name": o.receiver_name,
                        "receiver_phone": o.receiver_phone, "message": str(exc.detail),
                    })
                    continue
            order_data = build_order_data(o, db)
            result = (
                courier.precheck_order(order_data, o.express_type)
                if o.courier_type == "JDL"
                else courier.precheck_order(order_data)
            )
            o.order_response = json.dumps(result, ensure_ascii=False, default=str)
            if result.get("success"):
                o.status = "prechecked"
                passed += 1
                ticket_quotes.append({
                    "order_id": o.id,
                    "receiver_name": o.receiver_name,
                    "receiver_phone": o.receiver_phone,
                    "destination": "".join(filter(None, (
                        o.receiver_province, o.receiver_city, o.receiver_district,
                    ))),
                    "estimated_fee": o.freight,
                    "estimated_delivery": (
                        o.estimated_delivery_time.isoformat()
                        if o.estimated_delivery_time else None
                    ),
                })
            else:
                o.status = "validation_failed"
                failed += 1
                validation_errors.append({
                    "order_id": o.id, "receiver_name": o.receiver_name,
                    "receiver_phone": o.receiver_phone,
                    "message": result.get("message") or "预下单校验失败",
                })
        batch.committed_count = committed; batch.total_count = committed
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(400, f"入库失败, 已回滚: {e}")
    return {"ok": failed == 0, "batch_id": batch.id, "committed": committed,
            "prechecked": passed, "failed": failed, "errors": validation_errors[:20],
            "tickets": ticket_quotes,
            "estimated_total": round(sum(
                float(item["estimated_fee"] or 0) for item in ticket_quotes
            ), 2),
            "detail": (f"{failed} 条未通过预下单校验，请修正 Excel 或页面信息后重新导入" if failed else None)}


@router.post("/{batch_id}/quotes")
def refresh_batch_quotes(batch_id: int, body: dict, request: Request,
                         db: Session = Depends(get_db)):
    """Refresh every ticket after the shared fields or product are changed."""
    batch = db.query(models.Batch).filter(models.Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(404, "批次不存在")
    _assert_batch_owner(batch, request)
    if batch.status != "draft":
        raise HTTPException(400, f"当前状态 {batch.status} 不可刷新")
    orders = db.query(models.Order).filter(models.Order.batch_id == batch.id).all()
    if not orders:
        raise HTTPException(400, "请先完成首次逐票校验")

    _apply_batch_fields(batch, body.get("batch_fields") or {})
    from utils.regions import enabled_carriers, get_config
    if batch.courier_type not in enabled_carriers(db, batch.region_code):
        raise HTTPException(400, "当前工作区域未启用所选快递服务")
    billing_key = {
        "SF": "sf_monthly_card", "DB": "db_customer_code", "JDL": "jdl_customer_code",
    }.get(batch.courier_type)
    batch.billing_account_code = (
        get_config(db, batch.region_code, billing_key, "") if billing_key else None
    )

    from courier import get_courier
    from api.order_dispatch import build_order_data, parse_delivery
    from api.routes.courier import QuoteQuery, require_selected_service_quote
    from courier_services import ALL_SERVICES, available_in_region
    selected_service = next((service for service in ALL_SERVICES
        if service["courier"] == batch.courier_type
        and service["express_type"] == str(batch.express_type or "")
        and available_in_region(service, batch.region_code)), None)
    if not selected_service:
        raise HTTPException(400, "所选快递产品与当前区域或承运商不匹配")

    courier = get_courier(batch.courier_type, db, batch.region_code)
    can_quote = (
        batch.courier_type == "JDL"
        or callable(getattr(courier, "query_delivery_quotes", None))
    )
    tickets, errors = [], []
    for order in orders:
        _sync_order_from_batch(order, batch)
        order.status = "prechecking"
        try:
            if can_quote:
                quote = require_selected_service_quote(
                    selected_service["id"],
                    QuoteQuery(
                        sender_province=order.sender_province, sender_city=order.sender_city,
                        sender_district=order.sender_district, sender_address=order.sender_address,
                        receiver_province=order.receiver_province, receiver_city=order.receiver_city,
                        receiver_district=order.receiver_district, receiver_address=order.receiver_address,
                        item_type=order.item_name, weight=order.item_weight, volume=order.item_volume,
                        parcel_count=order.parcel_count, insurance_amount=order.insurance or 0,
                        pickup_start_time=(
                            order.pickup_start_time.isoformat()
                            if order.pickup_start_time else None
                        ),
                        same_day_required=False,
                    ), db, batch.region_code, prefer_live=True,
                )
                order.freight = quote.get("estimated_fee")
                order.estimated_delivery_time = parse_delivery(
                    quote.get("estimated_delivery")
                )
            order_data = build_order_data(order, db)
            result = (
                courier.precheck_order(order_data, order.express_type)
                if order.courier_type == "JDL"
                else courier.precheck_order(order_data)
            )
            order.order_response = json.dumps(result, ensure_ascii=False, default=str)
            if not result.get("success"):
                raise ValueError(result.get("message") or "预下单校验失败")
            order.status = "prechecked"
            tickets.append({
                "order_id": order.id,
                "receiver_name": order.receiver_name,
                "receiver_phone": order.receiver_phone,
                "destination": "".join(filter(None, (
                    order.receiver_province, order.receiver_city, order.receiver_district,
                ))),
                "estimated_fee": order.freight,
                "estimated_delivery": (
                    order.estimated_delivery_time.isoformat()
                    if order.estimated_delivery_time else None
                ),
            })
        except HTTPException as exc:
            order.status = "validation_failed"
            errors.append({"receiver_name": order.receiver_name, "message": str(exc.detail)})
        except Exception as exc:
            order.status = "validation_failed"
            errors.append({"receiver_name": order.receiver_name, "message": str(exc)})
    db.commit()
    if errors:
        raise HTTPException(422, {
            "message": f"{len(errors)} 票费用时效或预校验刷新失败",
            "errors": errors[:20],
        })
    return {
        "ok": True, "tickets": tickets,
        "estimated_total": round(sum(
            float(item["estimated_fee"] or 0) for item in tickets
        ), 2),
    }


@router.post("/{batch_id}/submit")
def submit_approval(batch_id: int, request: Request, background_tasks: BackgroundTasks,
                    db: Session = Depends(get_db)):
    batch = db.query(models.Batch).filter(models.Batch.id == batch_id).first()
    if not batch: raise HTTPException(404, "批次不存在")
    _assert_batch_owner(batch, request)
    if batch.status != "draft": raise HTTPException(400, f"当前状态 {batch.status} 不可提交")
    orders = db.query(models.Order).filter(models.Order.batch_id == batch.id).all()
    if not orders:
        raise HTTPException(400, "请先确认入库 (commit)")
    failed = [o for o in orders if o.status == "validation_failed"]
    if failed:
        raise HTTPException(400, f"仍有 {len(failed)} 条未通过预下单校验，不能提交审批")
    if any(o.status != "prechecked" for o in orders):
        raise HTTPException(400, "批次明细状态不完整，请重新导入")
    assign_approval(batch, db)
    for order in orders:
        assign_approval(order, db)
        order.status = "pending"
    batch.status = "pending_approval"; db.commit()
    notification = queue_approval_notification(background_tasks, db, batch, "batch")
    applicant_notification = queue_approval_progress_notification(
        background_tasks, db, batch, "submitted", "batch"
    )
    return {
        "ok": True, "total": len(orders), "notification": notification,
        "applicant_notification": applicant_notification,
    }


def _place_batch_orders(batch, db, allowed_statuses=None):
    from courier import get_courier_for_order
    from api.order_dispatch import build_order_data
    allowed_statuses = allowed_statuses or ["pending", "approved"]
    orders = db.query(models.Order).filter(
        models.Order.batch_id == batch.id,
        models.Order.status.in_(allowed_statuses),
    ).order_by(models.Order.id).all()
    batch.status = "ordering"; batch.ordered_count = 0; batch.failed_count = 0
    db.commit()
    success = failed = 0
    for order in orders:
        try:
            courier = get_courier_for_order(order, db)
            resp = courier.create_order(build_order_data(order, db))
            order.order_response = json.dumps(resp, ensure_ascii=False, default=str)
            if resp.get("success"):
                order.status = "ordered"
                order.courier_order_no = resp.get("courier_order_no")
                order.courier_order_nos = json.dumps(
                    resp.get("courier_order_nos") or ([order.courier_order_no] if order.courier_order_no else []),
                    ensure_ascii=False,
                )
                if resp.get("freight") is not None:
                    order.freight = resp.get("freight")
                order.route_code = resp.get("dest_code")
                order.order_time = dt.datetime.now()
                db.add(models.CourierTrack(
                    order_id=order.id, courier_order_no=order.courier_order_no,
                    track_time=dt.datetime.now(), track_status="已下单",
                    track_detail=(f"运单号 {order.courier_order_no}，等待揽收"
                                  if order.courier_order_no else "快递公司已受理，等待生成运单号"),
                ))
                success += 1
            else:
                order.status = "failed"; failed += 1
        except Exception as exc:
            order.status = "failed"; order.order_response = str(exc); failed += 1
        batch.ordered_count = success; batch.failed_count = failed
        db.commit()
    batch.status = "done" if failed == 0 else ("partial" if success > 0 else "failed")
    db.commit()
    return {"ok": failed == 0, "success": success, "failed": failed, "total": len(orders),
            "status": batch.status,
            "detail": (f"{failed} 票生产下单失败，请在订单详情查看承运商返回" if failed else None)}


def _finalize_approved_batch(batch, orders, remark, background_tasks, db):
    batch.status = "approved"
    for order in orders:
        order.status = "approved"
    db.commit()
    notification = queue_approval_result_notification(
        background_tasks, db, batch, True, remark or "", "batch"
    )
    result = _place_batch_orders(batch, db)
    dispatch_notification = queue_dispatch_notification(
        background_tasks, db, batch, result, "batch"
    )
    dispatch_ok = bool(result.get("ok"))
    failed_count = int(result.get("failed") or 0)
    result.update({
        "ok": True,
        "approval_completed": True,
        "dispatch_ok": dispatch_ok,
        "current_level": batch.approval_total_levels,
        "notification": notification,
        "dispatch_notification": dispatch_notification,
        "message": (
            "批次审批已通过，全部订单下单成功"
            if dispatch_ok
            else f"批次审批已通过，但有 {failed_count} 票正式下单失败，请查看对应订单的失败原因"
        ),
    })
    return result


@router.post("/{batch_id}/approve")
def approve_batch(batch_id: int, request: Request, background_tasks: BackgroundTasks,
                  body: dict = None, db: Session = Depends(get_db)):
    batch = db.query(models.Batch).filter(models.Batch.id == batch_id).first()
    if not batch: raise HTTPException(404, "批次不存在")
    if batch.status != "pending_approval": raise HTTPException(400, "当前状态不可审批")
    ensure_approval_snapshot(batch, db)
    actor_id = actor_id_from_request(request)
    assert_assigned_approver(
        batch, actor_id, bool(getattr(request.state, "auth_enabled", False))
    )
    remark = (body or {}).get("remark", "")
    orders = db.query(models.Order).filter(
        models.Order.batch_id == batch.id, models.Order.status == "pending"
    ).all()
    completed = approve_level(batch, actor_id, remark)
    for order in orders:
        ensure_approval_snapshot(order, db)
        approve_level(order, actor_id, remark)
    if not completed:
        db.commit()
        notification = queue_approval_notification(background_tasks, db, batch, "batch")
        applicant_notification = queue_approval_progress_notification(
            background_tasks, db, batch, "level1_approved", "batch"
        )
        return {
            "ok": True, "approval_completed": False,
            "current_level": batch.approval_current_level,
            "total_levels": batch.approval_total_levels,
            "message": "一级审批已通过，等待二级审批",
            "notification": notification,
            "applicant_notification": applicant_notification,
        }
    return _finalize_approved_batch(batch, orders, remark, background_tasks, db)


@router.post("/{batch_id}/skip/{level}")
def skip_batch_level(batch_id: int, level: int, request: Request,
                     background_tasks: BackgroundTasks, body: dict = None,
                     db: Session = Depends(get_db)):
    admin = _assert_admin_request(request, db)
    if level not in (1, 2):
        raise HTTPException(400, "审批层级只能是一级或二级")
    batch = db.query(models.Batch).filter(models.Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(404, "批次不存在")
    if batch.status != "pending_approval":
        raise HTTPException(400, "当前批次已不在待审批状态")
    ensure_approval_snapshot(batch, db)
    current = int(batch.approval_current_level or 1)
    total = int(batch.approval_total_levels or 1)
    if level > total:
        raise HTTPException(400, f"当前审批流没有第{level}级审批")
    if current != level:
        raise HTTPException(400, f"当前正在等待第{current}级审批，不能跳过第{level}级")
    actor_id = int(admin["id"])
    remark = _skip_remark(
        level, str(admin.get("name") or "管理员"), (body or {}).get("remark", "")
    )
    orders = db.query(models.Order).filter(
        models.Order.batch_id == batch.id, models.Order.status == "pending"
    ).all()
    setattr(batch, f"approval_level{level}_user_id", actor_id)
    completed = approve_level(batch, actor_id, remark)
    for order in orders:
        ensure_approval_snapshot(order, db)
        setattr(order, f"approval_level{level}_user_id", actor_id)
        approve_level(order, actor_id, remark)
    if not completed:
        db.commit()
        notification = queue_approval_notification(background_tasks, db, batch, "batch")
        applicant_notification = queue_approval_progress_notification(
            background_tasks, db, batch, "level1_approved", "batch"
        )
        return {
            "ok": True, "approval_completed": False,
            "current_level": batch.approval_current_level,
            "total_levels": batch.approval_total_levels,
            "message": "已跳过一级审批，等待二级审批",
            "notification": notification,
            "applicant_notification": applicant_notification,
        }
    return _finalize_approved_batch(batch, orders, remark, background_tasks, db)


@router.post("/{batch_id}/reject")
def reject_batch(batch_id: int, request: Request, background_tasks: BackgroundTasks,
                 body: dict = None, db: Session = Depends(get_db)):
    batch = db.query(models.Batch).filter(models.Batch.id == batch_id).first()
    if not batch: raise HTTPException(404, "批次不存在")
    if batch.status != "pending_approval": raise HTTPException(400, "当前状态不可拒绝")
    ensure_approval_snapshot(batch, db)
    actor_id = actor_id_from_request(request)
    assert_assigned_approver(
        batch, actor_id, bool(getattr(request.state, "auth_enabled", False))
    )
    remark = (body or {}).get("remark", "")
    reject_level(batch, actor_id, remark)
    batch.status = "rejected"
    orders = db.query(models.Order).filter(models.Order.batch_id == batch.id).all()
    for order in orders:
        ensure_approval_snapshot(order, db)
        reject_level(order, actor_id, remark)
        order.status = "rejected"
    db.commit()
    notification = queue_approval_result_notification(
        background_tasks, db, batch, False, remark, "batch"
    )
    return {"ok": True, "notification": notification}


@router.post("/{batch_id}/direct-order")
def direct_order_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    production_confirmation: Optional[str] = Header(
        default=None, alias="X-Confirm-Production-Order"
    ),
):
    """生产直下已停用；所有批次必须完成配置的审批流程。"""
    raise HTTPException(403, "生产直下已禁用，所有批次必须提交审批")


@router.post("/{batch_id}/order")
def order_batch(batch_id: int):
    """Final approval already places the batch; prevent a second/manual bypass."""
    raise HTTPException(403, "批次审批完成后会自动下单，请勿手动重复下单")


# ====== 列表 / 详情 / 删除 ======

@router.get("")
def list_batches(request: Request, db: Session = Depends(get_db), user_id: int = Query(None),
                 status: str = Query(None), limit: int = Query(50, ge=1, le=200)):
    q = db.query(models.Batch).order_by(models.Batch.created_at.desc())
    if _request_auth_enabled(request) and not _is_admin_request(request):
        q = q.filter(models.Batch.created_by == _current_user_id(request))
    elif user_id:
        q = q.filter(models.Batch.created_by == user_id)
    if status:
        q = q.filter(models.Batch.status == status)
    else:
        q = q.filter(models.Batch.status != "draft")
    return {"ok": True, "items": [_to_dict(b) for b in q.limit(limit).all()]}


@router.get("/{batch_id}")
def get_batch(batch_id: int, request: Request, db: Session = Depends(get_db)):
    b = db.query(models.Batch).filter(models.Batch.id == batch_id).first()
    if not b: raise HTTPException(404, "批次不存在")
    _assert_batch_readable(b, request)
    return {"ok": True, "batch": _to_dict(b, db)}


@router.delete("/{batch_id}")
def delete_batch(batch_id: int, request: Request, db: Session = Depends(get_db)):
    b = db.query(models.Batch).filter(models.Batch.id == batch_id).first()
    if not b: raise HTTPException(404, "批次不存在")
    _assert_batch_owner(b, request)
    if b.status not in ("draft", "rejected"): raise HTTPException(400, f"状态 {b.status} 不可删除")
    db.query(models.Order).filter(models.Order.batch_id == batch_id).delete()
    db.delete(b); db.commit()
    return {"ok": True}
