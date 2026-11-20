from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator
from typing import Optional
from fastapi import BackgroundTasks, Request
from database.connection import get_db
from utils.approval_policy import assign_approval
from utils.approval_notifications import (
    queue_approval_notification, queue_approval_progress_notification,
    queue_sf_city_status_notification, sf_city_event_from_transition,
    clear_sf_city_notification,
)
from utils.sender_addresses import assert_sender_address_allowed
from utils.order_status import (
    apply_latest_track_state, classify_order_status, order_status_display,
    preferred_track_status,
)
from utils.order_failure import extract_order_failure
from database import models
import datetime as dt, json, re, uuid

router = APIRouter()


def _visible_orders_query(db: Session):
    draft_batch_ids = db.query(models.Batch.id).filter(models.Batch.status == "draft")
    return db.query(models.Order).filter(or_(
        models.Order.batch_id.is_(None),
        ~models.Order.batch_id.in_(draft_batch_ids),
    ))


def order_can_resubmit(order) -> bool:
    """Only applications that never produced a waybill can safely be resubmitted."""
    return (
        str(getattr(order, "status", "") or "").lower() in {"rejected", "failed"}
        and not str(getattr(order, "courier_order_no", "") or "").strip()
    )


def order_can_delete(order) -> bool:
    status = str(getattr(order, "status", "") or "").lower()
    has_waybill = bool(str(getattr(order, "courier_order_no", "") or "").strip())
    if status == "cancelled":
        return True
    return not has_waybill and status in {
        "rejected", "failed", "validation_failed", "prechecked",
    }


def _request_auth_enabled(request: Request) -> bool:
    return bool(getattr(request.state, "auth_enabled", False))


def _current_user_id(request: Request) -> Optional[int]:
    current_user = getattr(request.state, "current_user", None) or {}
    return int(current_user["id"]) if current_user.get("id") is not None else None


def _is_admin_request(request: Request) -> bool:
    current_user = getattr(request.state, "current_user", None) or {}
    return str(current_user.get("role") or "employee") == "admin"


def _assert_order_readable(order, request: Request) -> None:
    """Employees may read their own applications and approvals assigned to them."""
    if not _request_auth_enabled(request) or _is_admin_request(request):
        return
    user_id = _current_user_id(request)
    allowed_ids = {
        getattr(order, "created_by", None),
        getattr(order, "approver_id", None),
        getattr(order, "approval_level1_user_id", None),
        getattr(order, "approval_level2_user_id", None),
    }
    if user_id not in allowed_ids:
        raise HTTPException(403, "无权查看该订单")


def _assert_order_mutable(order, request: Request) -> None:
    """Only an order owner or administrator may trigger mutations/carrier calls."""
    if not _request_auth_enabled(request) or _is_admin_request(request):
        return
    if _current_user_id(request) != getattr(order, "created_by", None):
        raise HTTPException(403, "无权操作该订单")


def _normalized_track_text(value):
    return " ".join(str(value or "").split())


_SF_CITY_INTERNAL_MARKER = re.compile(r"\[sfcity:[0-9a-f]+\]\s*", re.I)


def _public_track_text(value):
    """Remove callback deduplication metadata from employee-facing copy."""
    return _SF_CITY_INTERNAL_MARKER.sub("", str(value or "")).strip()


def _track_key(track):
    """Carrier event identity; polling time is deliberately excluded."""
    if isinstance(track, dict):
        status, detail = track.get("status"), track.get("detail")
    else:
        status, detail = track.track_status, track.track_detail
    return _normalized_track_text(status), _normalized_track_text(detail)


def _track_time(value):
    if not value:
        return dt.datetime.now()
    if isinstance(value, dt.datetime):
        return value
    raw = str(value).strip()
    if raw.isdigit():
        timestamp = int(raw)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return dt.datetime.fromtimestamp(timestamp)
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("/", "-").replace("Z", "+00:00"))
    except ValueError:
        # One malformed carrier timestamp must not discard the whole tracking refresh.
        return dt.datetime.now()
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _save_new_tracks(db, order, api_tracks):
    """Persist only genuinely new carrier states, not repeated polling results."""
    existing_keys = {
        _track_key(track) for track in db.query(models.CourierTrack).filter(
            models.CourierTrack.order_id == order.id
        ).all()
    }
    inserted = []
    for item in api_tracks or []:
        _update_courier_assignment(order, item)
        key = _track_key(item)
        if key in existing_keys:
            continue
        db.add(models.CourierTrack(
            order_id=order.id,
            courier_order_no=order.courier_order_no,
            track_time=_track_time(item.get("time")),
            track_status=item.get("status", ""),
            track_detail=item.get("detail", ""),
        ))
        existing_keys.add(key)
        inserted.append(item)
    return inserted


def _latest_tracks_by_order(db, orders):
    order_ids = [order.id for order in orders if order.id is not None]
    if not order_ids:
        return {}
    tracks = db.query(models.CourierTrack).filter(
        models.CourierTrack.order_id.in_(order_ids)
    ).order_by(
        models.CourierTrack.order_id,
        models.CourierTrack.track_time.desc(),
        models.CourierTrack.id.desc(),
    ).all()
    latest = {}
    for track in tracks:
        latest.setdefault(track.order_id, track)
    return latest


def _serialize_unique_tracks(tracks, courier_type=""):
    """Hide historical duplicate rows while retaining the newest occurrence."""
    result, seen = [], set()
    rows = list(tracks or [])
    sf_city = str(courier_type or "").upper() == "SF_CITY"
    authoritative_assignment_times = []
    if sf_city:
        for row in rows:
            raw_detail = row.get("detail", "") if isinstance(row, dict) else row.track_detail
            raw_status = row.get("status", "") if isinstance(row, dict) else row.track_status
            when = row.get("time") if isinstance(row, dict) else row.track_time
            text = f"{raw_status or ''} {raw_detail or ''}"
            if "[sfcity:" in str(raw_detail or "").lower() and any(
                word in text for word in ("已指派", "已接单", "已到店", "已取货", "完成")
            ):
                authoritative_assignment_times.append(_track_time(when))
    for track in tracks:
        raw_status = track.get("status", "") if isinstance(track, dict) else track.track_status
        raw_detail = track.get("detail", "") if isinstance(track, dict) else track.track_detail
        raw_time = track.get("time") if isinstance(track, dict) else track.track_time
        # Hide the one legacy false node produced before the parser was fixed:
        # an unmarked "arrived" record that predates the first authoritative
        # SF City assignment callback is an impossible state transition.
        if (
            sf_city
            and "已到店" in f"{raw_status or ''} {raw_detail or ''}"
            and "[sfcity:" not in str(raw_detail or "").lower()
            and authoritative_assignment_times
            and _track_time(raw_time) < min(authoritative_assignment_times)
        ):
            continue
        key = _track_key(track)
        if key in seen:
            continue
        seen.add(key)
        if isinstance(track, dict):
            result.append({
                **track,
                "status": _public_track_text(track.get("status")),
                "detail": _public_track_text(track.get("detail")),
            })
        else:
            result.append({
                "time": track.track_time.isoformat() if track.track_time else None,
                "status": _public_track_text(track.track_status),
                "detail": _public_track_text(track.track_detail),
            })
    return result


def _refresh_order_tracking(db, order, include_freight=True):
    """Refresh one shipped order and persist its latest carrier state."""
    from courier import get_courier_for_order

    courier = get_courier_for_order(order, db)
    tracks = courier.query_track(order.courier_order_no) or []
    inserted_tracks = _save_new_tracks(db, order, tracks)
    previous_status = order.status
    if tracks:
        latest_track = max(tracks, key=lambda item: _track_time(item.get("time")))
        apply_latest_track_state(order, latest_track)
    order.track_update_time = dt.datetime.now()

    freight_message = ""
    if include_freight and order.courier_type in ("SF", "SF_CITY", "DB", "JDL"):
        try:
            fee_result = courier.query_actual_freight(order.courier_order_no)
            freight_message = fee_result.get("message") or ""
            if fee_result.get("success") and fee_result.get("freight") is not None:
                order.actual_freight = fee_result["freight"]
                fee_detail = fee_result.get("fee_items") or fee_result.get("fee_detail") or {}
                order.actual_fee_detail = json.dumps(fee_detail, ensure_ascii=False)
        except Exception as exc:
            # 运费查询失败不能阻止已经取得的物流状态入库。
            freight_message = f"实际运费查询失败：{exc}"
    return {
        "tracks": tracks,
        "inserted_tracks": inserted_tracks,
        "previous_status": previous_status,
        "order_status": order.status,
        "actual_freight": order.actual_freight,
        "freight_message": freight_message,
    }


class OrderCreate(BaseModel):
    courier_type: str
    sender_name: str
    sender_phone: str
    sender_company: Optional[str] = None
    sender_province: str
    sender_city: str
    sender_district: str
    sender_address: str
    receiver_name: str
    receiver_phone: str
    receiver_company: Optional[str] = None
    receiver_province: str
    receiver_city: str
    receiver_district: str
    receiver_address: str
    item_name: str
    item_weight: float
    item_volume: Optional[float] = None
    insurance_amount: float = 0
    estimated_freight: Optional[float] = None
    parcel_count: int = 1
    item_count: int = 1
    item_remark: Optional[str] = None
    pickup_time_mode: str = "immediate"
    pickup_start_time: Optional[str] = None
    pickup_end_time: Optional[str] = None
    shipping_reason: Optional[str] = None
    express_type: Optional[str] = None
    express_type_name: Optional[str] = None
    cost_type: Optional[str] = None
    dept_code: Optional[str] = None
    dept_name: Optional[str] = None
    project_code: Optional[str] = None
    project_name: Optional[str] = None
    source: Optional[str] = "system"
    source_id: Optional[str] = None
    created_by: Optional[int] = None
    save_receiver_address: bool = False
    debug_region_code: Optional[str] = None

    @field_validator("created_by", mode="before")
    @classmethod
    def empty_created_by_is_none(cls, value):
        return None if value == "" else value

    @field_validator("sender_phone", "receiver_phone", mode="before")
    @classmethod
    def validate_contact_phone(cls, value):
        from utils.contact import normalize_contact_number
        return normalize_contact_number(value)


class SFCityTipReq(BaseModel):
    amount: int

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value):
        if int(value) < 1 or int(value) > 200:
            raise ValueError("小费金额必须为 1 至 200 元的整数")
        return int(value)


@router.post("")
def create_order(order: OrderCreate, request: Request, background_tasks: BackgroundTasks,
                 db: Session = Depends(get_db)):
    current_user = getattr(request.state, "current_user", None)
    from utils.regions import enabled_carriers, get_config, request_region
    region_code = request_region(
        db, current_user, order.debug_region_code,
        auth_enabled=_request_auth_enabled(request),
    )
    if current_user:
        order.created_by = current_user["id"]
    assert_sender_address_allowed(db, current_user, order.model_dump())
    if order.cost_type:
        normalized_cost_type = str(order.cost_type).strip().lower()
        if normalized_cost_type in {"部门", "department"}:
            order.cost_type = "department"
            order.project_code = None
            order.project_name = None
        elif normalized_cost_type in {"项目", "project"}:
            order.cost_type = "project"
            if not str(order.project_name or "").strip():
                raise HTTPException(400, "选择项目归属时必须上传项目")
        else:
            raise HTTPException(400, "费用归属只能是部门或项目")
        if current_user and not str(order.dept_name or "").strip():
            order.dept_name = current_user.get("department") or None
        if not str(order.dept_name or "").strip():
            raise HTTPException(400, "必须上传费用归属部门")
    courier_type = order.courier_type.upper().strip()
    if courier_type not in {"SF", "DB", "JDL"}:
        raise HTTPException(400, "不支持该快递公司")
    if courier_type not in enabled_carriers(db, region_code):
        raise HTTPException(400, "当前工作区域未启用该快递服务")
    if order.parcel_count > 1 and courier_type not in {"SF", "JDL"}:
        raise HTTPException(400, "多包裹下单目前只支持顺丰和京东，请重新选择产品")
    if order.item_count < 1:
        raise HTTPException(400, "item_count 必须大于 0")
    if order.parcel_count < 1 or order.parcel_count > 307:
        raise HTTPException(400, "包裹数量必须是1至307之间的整数")
    if order.item_weight <= 0:
        raise HTTPException(400, "预估重量必须大于 0")
    if order.item_volume is not None and order.item_volume <= 0:
        raise HTTPException(400, "预估体积必须大于 0")
    if order.insurance_amount < 0:
        raise HTTPException(400, "保价金额不能小于 0")
    if courier_type == "JDL" and order.insurance_amount > 300000:
        raise HTTPException(400, "京东快递普通保价声明价值不能超过300000元")
    if order.estimated_freight is not None and order.estimated_freight < 0:
        raise HTTPException(400, "预估运费不能小于 0")
    from courier_services import ALL_SERVICES, available_in_region
    requested_express_type = str(order.express_type or ("2" if courier_type == "SF" else "XJTK"))
    selected_service = next(
        (service for service in ALL_SERVICES
         if service["courier"] == courier_type
         and service["express_type"] == requested_express_type
         and available_in_region(service, region_code)),
        None,
    )
    if not selected_service:
        raise HTTPException(400, "所选快递产品与承运商不匹配，请重新选择快递方式")
    order.express_type = selected_service["express_type"]
    order.express_type_name = selected_service["name"]
    from api.address_validation import validate_division
    for label, province, city, district in (
        ("寄件地址", order.sender_province, order.sender_city, order.sender_district),
        ("收件地址", order.receiver_province, order.receiver_city, order.receiver_district),
    ):
        division_error = validate_division(province, city, district)
        if division_error:
            raise HTTPException(400, f"{label}无效：{division_error}，请从标准省市区中重新选择")
    if order.pickup_time_mode not in {"immediate", "appointment"}:
        raise HTTPException(400, "上门时间必须选择立即上门或预约上门")

    pickup_start = pickup_end = None
    if order.pickup_time_mode == "appointment":
        try:
            pickup_start = dt.datetime.fromisoformat(order.pickup_start_time or "")
            pickup_end = dt.datetime.fromisoformat(order.pickup_end_time or "")
        except ValueError:
            raise HTTPException(400, "预约上门必须填写有效的开始和截止时间")
        now = dt.datetime.now()
        if pickup_start <= now:
            raise HTTPException(400, "预约开始时间必须晚于当前时间")
        if pickup_end <= pickup_start:
            raise HTTPException(400, "预约截止时间必须晚于开始时间")
        if (
            pickup_start.minute != 0
            or pickup_start.second != 0
            or pickup_start.hour not in {8, 10, 12, 14, 16, 18}
            or pickup_end - pickup_start != dt.timedelta(hours=2)
        ):
            raise HTTPException(400, "预约时间必须选择系统提供的两小时时段")
        if pickup_start.date() > now.date() + dt.timedelta(days=2):
            raise HTTPException(400, "预约日期只能选择今天、明天或后天")

    # 前端禁用状态只能改善交互，不能作为下单依据；提交时重新向承运商核验所选产品。
    from api.routes.courier import QuoteQuery, require_selected_service_quote
    selected_quote = require_selected_service_quote(
        selected_service["id"],
        QuoteQuery(
            sender_province=order.sender_province, sender_city=order.sender_city,
            sender_district=order.sender_district, sender_address=order.sender_address,
            receiver_province=order.receiver_province, receiver_city=order.receiver_city,
            receiver_district=order.receiver_district, receiver_address=order.receiver_address,
            item_type=order.item_name, weight=order.item_weight, volume=order.item_volume,
            parcel_count=order.parcel_count, insurance_amount=order.insurance_amount,
            pickup_start_time=(pickup_start.isoformat() if pickup_start else None),
            same_day_required=False,
        ),
        db, region_code,
    )
    order.estimated_freight = selected_quote.get("estimated_fee")

    # UUID 保证高强度随机，数据库唯一索引是最终防重屏障。
    order_no = "CO" + uuid.uuid4().hex.upper()

    # 顺丰预下单只做校验，不创建真实订单。校验失败时不写入本地订单。
    if courier_type in {"SF", "JDL"}:
        from courier import get_courier
        precheck_data = order.model_dump()
        precheck_data.update({
            "order_no": order_no,
            "pickup_start_time": pickup_start.strftime("%Y-%m-%d %H:%M:%S") if pickup_start else None,
            "pickup_end_time": pickup_end.strftime("%Y-%m-%d %H:%M:%S") if pickup_end else None,
        })
        result = get_courier(courier_type, db, region_code).precheck_order(
            precheck_data, order.express_type
        ) if courier_type == "JDL" else get_courier("SF", db, region_code).precheck_order(precheck_data)
        if not result.get("success"):
            code = result.get("error_code") or ""
            message = result.get("message") or "顺丰预下单未通过"
            if code == "CONFIGURATION":
                raise HTTPException(422, f"{message}。请先到系统设置补充当前区域的快递接口配置。")
            suffix = f"（{code}）" if code else ""
            raise HTTPException(422, f"{selected_service['name']}预下单未通过：{message}{suffix}。请修改地址或订单信息后重试。")

    billing_key = {
        "SF": "sf_monthly_card", "DB": "db_customer_code", "JDL": "jdl_customer_code",
    }[courier_type]

    o = models.Order(
        order_no=order_no, courier_type=courier_type, region_code=region_code,
        billing_account_code=get_config(db, region_code, billing_key, "") or None,
        sender_name=order.sender_name, sender_phone=order.sender_phone,
        sender_company=order.sender_company, sender_province=order.sender_province,
        sender_city=order.sender_city, sender_district=order.sender_district,
        sender_address=order.sender_address,
        receiver_name=order.receiver_name, receiver_phone=order.receiver_phone,
        receiver_company=order.receiver_company, receiver_province=order.receiver_province,
        receiver_city=order.receiver_city, receiver_district=order.receiver_district,
        receiver_address=order.receiver_address,
        item_name=order.item_name, item_weight=order.item_weight, item_volume=order.item_volume,
        insurance=order.insurance_amount, parcel_count=order.parcel_count,
        freight=order.estimated_freight,
        item_count=order.item_count,
        item_remark=order.item_remark, shipping_reason=order.shipping_reason,
        pickup_time_mode=order.pickup_time_mode,
        pickup_start_time=pickup_start, pickup_end_time=pickup_end,
        express_type=order.express_type, express_type_name=order.express_type_name,
        cost_type=order.cost_type, dept_code=order.dept_code, dept_name=order.dept_name,
        project_code=order.project_code, project_name=order.project_name,
        status="pending", source=order.source, source_id=order.source_id,
        created_by=order.created_by,
    )
    assign_approval(o, db)
    db.add(o)
    receiver_address_saved = False
    if order.save_receiver_address:
        user_id = int(current_user["id"]) if current_user else order.created_by
        if not user_id:
            raise HTTPException(400, "保存个人地址簿需要先登录")
        existing_address = db.query(models.Address).filter(
            models.Address.user_id == user_id,
            models.Address.scope == "personal",
            models.Address.role == "receiver",
            models.Address.phone == order.receiver_phone,
            models.Address.province == order.receiver_province,
            models.Address.city == order.receiver_city,
            models.Address.district == order.receiver_district,
            models.Address.detail == order.receiver_address,
        ).first()
        if not existing_address:
            db.add(models.Address(
                user_id=user_id, scope="personal", role="receiver",
                label=order.receiver_name, name=order.receiver_name,
                phone=order.receiver_phone, company_name=order.receiver_company,
                province=order.receiver_province, city=order.receiver_city,
                district=order.receiver_district, detail=order.receiver_address,
            ))
        receiver_address_saved = True
    db.commit(); db.refresh(o)
    notification = queue_approval_notification(background_tasks, db, o, "order")
    applicant_notification = queue_approval_progress_notification(
        background_tasks, db, o, "submitted", "order"
    )
    return {"ok": True, "order_no": order_no, "id": o.id,
            "notification": notification,
            "applicant_notification": applicant_notification,
            "receiver_address_saved": receiver_address_saved}


@router.post("/direct")
def create_direct_order(
    order: OrderCreate,
    request: Request,
    db: Session = Depends(get_db),
    production_confirmation: Optional[str] = Header(
        default=None, alias="X-Confirm-Production-Order"
    ),
):
    """生产直下已停用；所有订单必须完成配置的审批流程。"""
    raise HTTPException(403, "生产直下已禁用，所有订单必须提交审批")

@router.get("")
def list_orders(request: Request, status: Optional[str] = None, courier_type: Optional[str] = None,
                keyword: Optional[str] = None, user_id: Optional[int] = None,
                limit: int = Query(50, ge=1, le=200),
                db: Session = Depends(get_db)):
    q = _visible_orders_query(db)
    if status: q = q.filter(models.Order.status == status)
    if courier_type: q = q.filter(models.Order.courier_type == courier_type.upper())
    if _request_auth_enabled(request) and not _is_admin_request(request):
        q = q.filter(models.Order.created_by == _current_user_id(request))
    elif user_id:
        q = q.filter(models.Order.created_by == user_id)
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(models.Order.order_no.like(like) | models.Order.receiver_name.like(like)
                      | models.Order.receiver_phone.like(like) | models.Order.courier_order_no.like(like))
    orders = q.order_by(models.Order.created_at.desc()).limit(limit).all()
    latest_tracks = _latest_tracks_by_order(db, orders)
    return {
        "ok": True,
        "items": [_to_dict(order, latest_tracks.get(order.id)) for order in orders],
    }


@router.get("/status-snapshot")
def order_status_snapshot(
    request: Request,
    order_ids: str = Query("", description="逗号分隔的订单ID"),
    db: Session = Depends(get_db),
):
    """Return canonical states for rows already visible in the browser."""
    ids = []
    for value in str(order_ids or "").split(","):
        value = value.strip()
        if value.isdigit():
            ids.append(int(value))
    ids = list(dict.fromkeys(ids))[:500]
    if not ids:
        return {"ok": True, "items": []}

    query = db.query(models.Order).filter(models.Order.id.in_(ids))
    if _request_auth_enabled(request) and not _is_admin_request(request):
        query = query.filter(models.Order.created_by == _current_user_id(request))
    orders = query.all()
    items = []
    for order in orders:
        # track_status is the denormalized latest carrier state maintained by
        # every automatic/manual refresh. Do not scan the growing track table
        # for this once-per-minute browser heartbeat.
        display = order_status_display(
            order.status, order.track_status or "", bool(order.courier_order_no)
        )
        items.append({
            "id": order.id,
            "status": order.status,
            "status_category": display["status_category"],
            "status_label": display["status_label"],
            "track_status": order.track_status or "",
            "track_update_time": (
                order.track_update_time.isoformat() if order.track_update_time else None
            ),
        })
    return {"ok": True, "items": items}


@router.get("/stats")
def stats(request: Request, db: Session = Depends(get_db)):
    def scoped_query():
        query = _visible_orders_query(db)
        if _request_auth_enabled(request) and not _is_admin_request(request):
            query = query.filter(models.Order.created_by == _current_user_id(request))
        return query

    by_status = {}
    for s in ["pending", "approved", "ordered", "success", "delivered", "rejected", "cancelled", "failed"]:
        by_status[s] = scoped_query().filter(models.Order.status == s).count()
    return {"ok": True, "total": scoped_query().count(), "by_status": by_status,
            "by_courier": {"SF": scoped_query().filter(models.Order.courier_type == "SF").count(),
                           "DB": scoped_query().filter(models.Order.courier_type == "DB").count()}}


@router.post("/refresh-all-tracks")
def refresh_all_tracks(request: Request, background_tasks: BackgroundTasks,
                       scope: str = Query("mine"), db: Session = Depends(get_db)):
    """Refresh every active shipped order visible to the current page."""
    current_user = getattr(request.state, "current_user", None)
    role = str((current_user or {}).get("role") or "employee")
    if scope not in {"mine", "all"}:
        raise HTTPException(400, "scope 只能是 mine 或 all")
    if scope == "all":
        from config.settings import settings
        if settings.dingtalk_login_enabled and role != "admin":
            raise HTTPException(403, "仅管理员可以刷新全部员工订单")

    query = db.query(models.Order).filter(
        models.Order.status.in_(["ordered", "success", "failed"]),
        models.Order.courier_order_no.isnot(None),
    )
    if scope == "mine" and current_user:
        query = query.filter(models.Order.created_by == int(current_user["id"]))
    orders_to_refresh = query.order_by(
        models.Order.track_update_time.is_(None).desc(),
        models.Order.track_update_time.asc(),
        models.Order.id.asc(),
    ).all()

    refreshed = delivered = 0
    failures = []
    for order in orders_to_refresh:
        try:
            previous_rider = order.courier_picked_by
            previous_track_status = order.track_status
            result = _refresh_order_tracking(db, order, include_freight=False)
            db.commit()
            lifecycle_event = sf_city_event_from_transition(
                order, result["previous_status"], previous_rider,
                previous_track_status,
            )
            if lifecycle_event:
                queue_sf_city_status_notification(
                    background_tasks, db, order, lifecycle_event,
                    order.track_status or "",
                )
            refreshed += 1
            if result["previous_status"] != "delivered" and result["order_status"] == "delivered":
                delivered += 1
        except Exception as exc:
            db.rollback()
            failures.append({
                "order_id": order.id,
                "order_no": order.order_no,
                "message": str(exc),
            })
    return {
        "ok": True,
        "total": len(orders_to_refresh),
        "refreshed": refreshed,
        "delivered": delivered,
        "failed": len(failures),
        "failures": failures[:10],
    }


@router.get("/{order_id}")
def get_order(order_id: int, request: Request, db: Session = Depends(get_db)):
    o = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not o: raise HTTPException(404, "Not found")
    _assert_order_readable(o, request)
    tracks = db.query(models.CourierTrack).filter(models.CourierTrack.order_id == order_id)\
              .order_by(models.CourierTrack.track_time.desc()).all()
    order_data = _to_dict(o, tracks[0] if tracks else None)
    creator = db.query(models.User).filter(models.User.id == o.created_by).first() if o.created_by else None
    approver = db.query(models.User).filter(models.User.id == o.approver_id).first() if o.approver_id else None
    level1_user = db.query(models.User).filter(models.User.id == o.approval_level1_user_id).first() if o.approval_level1_user_id else None
    level2_user = db.query(models.User).filter(models.User.id == o.approval_level2_user_id).first() if o.approval_level2_user_id else None
    order_data["created_by_name"] = creator.name if creator else None
    order_data["created_by_department"] = creator.department if creator else None
    order_data["approver_name"] = approver.name if approver else None
    order_data["approver_department"] = approver.department if approver else None
    order_data["approval_level1_user_name"] = level1_user.name if level1_user else None
    order_data["approval_level2_user_name"] = level2_user.name if level2_user else None
    return {"ok": True, "order": order_data,
            "tracks": _serialize_unique_tracks(tracks, o.courier_type)}


@router.delete("/{order_id}")
def delete_order_record(order_id: int, request: Request, db: Session = Depends(get_db)):
    order = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not order:
        raise HTTPException(404, "订单记录不存在")
    _assert_order_mutable(order, request)
    if not order_can_delete(order):
        if order.status == "pending":
            raise HTTPException(400, "请先撤回待审批申请，再删除记录")
        if order.courier_order_no and order.status != "cancelled":
            raise HTTPException(400, "该运单仍是有效承运记录，请先取消承运商订单后再删除")
        raise HTTPException(400, "当前状态不能删除；仅可删除已取消、已驳回或未生成运单的失败记录")

    batch = (
        db.query(models.Batch).filter(models.Batch.id == order.batch_id).first()
        if order.batch_id else None
    )
    db.query(models.CourierTrack).filter(
        models.CourierTrack.order_id == order.id
    ).delete(synchronize_session=False)
    db.delete(order)
    db.flush()
    if batch:
        remaining = db.query(models.Order).filter(models.Order.batch_id == batch.id).all()
        batch.total_count = len(remaining)
        batch.committed_count = len(remaining)
        batch.ordered_count = sum(1 for item in remaining if item.courier_order_no)
        batch.failed_count = sum(1 for item in remaining if item.status == "failed")
        if not remaining and batch.status in {"draft", "rejected", "failed"}:
            db.delete(batch)
    db.commit()
    return {"ok": True, "message": "记录已删除"}



@router.post("/{order_id}/refresh-track")
def refresh_track(order_id: int, request: Request,
                  background_tasks: BackgroundTasks,
                  db: Session = Depends(get_db)):
    """主动调用快递API查询最新轨迹并更新数据库"""
    o = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not o:
        raise HTTPException(404, "Not found")
    _assert_order_mutable(o, request)
    if not o.courier_order_no:
        raise HTTPException(400, "Order not shipped yet")

    try:
        previous_rider = o.courier_picked_by
        previous_track_status = o.track_status
        result = _refresh_order_tracking(db, o, include_freight=True)
        db.commit()
        lifecycle_event = sf_city_event_from_transition(
            o, result["previous_status"], previous_rider, previous_track_status
        )
        if lifecycle_event:
            queue_sf_city_status_notification(
                background_tasks, db, o, lifecycle_event, o.track_status or ""
            )

        return {"ok": True, "tracks": _serialize_unique_tracks(result["tracks"], o.courier_type),
                "new_track_count": len(result["inserted_tracks"]), "order_status": o.status,
                "actual_freight": result["actual_freight"],
                "freight_message": result["freight_message"]}
    except Exception as e: raise HTTPException(400, str(e))

@router.get("/{order_id}/track")
def get_track(order_id: int, request: Request, db: Session = Depends(get_db)):
    """查询订单的物流轨迹（先从DB读，没有则主动查快递API）"""
    o = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not o:
        raise HTTPException(404, "Not found")
    _assert_order_readable(o, request)

    # 先查DB
    tracks = db.query(models.CourierTrack).filter(
        models.CourierTrack.order_id == order_id
    ).order_by(models.CourierTrack.track_time.desc()).all()

    # DB里没记录但有快递单号 -> 主动查API
    if not tracks and o.courier_order_no:
        try:
            from courier import get_courier_for_order
            courier = get_courier_for_order(o, db)
            api_tracks = courier.query_track(o.courier_order_no)
            _save_new_tracks(db, o, api_tracks)
            if api_tracks:
                latest_track = max(api_tracks, key=lambda item: _track_time(item.get("time")))
                apply_latest_track_state(o, latest_track)
                o.track_update_time = dt.datetime.now()
            db.commit()
            tracks = db.query(models.CourierTrack).filter(
                models.CourierTrack.order_id == order_id
            ).order_by(models.CourierTrack.track_time.desc()).all()
        except Exception:
            tracks = []

    # 统一输出格式
    track_list = _serialize_unique_tracks(tracks, o.courier_type)

    return {"ok": True, "order_no": o.order_no, "courier_type": o.courier_type,
            "courier_order_no": o.courier_order_no, "tracks": track_list}

@router.post("/{order_id}/cancel")
def cancel_order(order_id: int, request: Request, db: Session = Depends(get_db)):
    o = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not o:
        raise HTTPException(404, "Not found")
    _assert_order_mutable(o, request)
    if o.status == "cancelled":
        return {"ok": True, "message": "订单已经取消"}
    if o.status in {"delivered", "rejected"}:
        raise HTTPException(400, f"当前状态 {o.status} 不可取消")

    current_category = classify_order_status(
        o.status, o.track_status or "", bool(o.courier_order_no)
    )
    if current_category in {"picked", "transit", "delivering", "delivered"}:
        raise HTTPException(
            400,
            "包裹已揽收或进入运输环节，不能再调用下单取消接口；"
            "请联系对应快递公司申请拦截、退回或售后处理，系统不会将订单改为已取消。",
        )

    # 尚未调用快递公司时，只取消本地申请。
    if o.status == "pending":
        o.status = "cancelled"
        db.commit()
        return {"ok": True, "message": "本地寄件申请已取消"}

    # 已经调用过承运商，必须以承运商取消成功为准，不能只改本地状态。
    if not o.courier_order_no and o.status != "failed":
        raise HTTPException(400, "快递公司已受理但尚无运单号，请先查询订单状态后再取消")
    from courier import get_courier_for_order
    result = get_courier_for_order(o, db).cancel_order(o.courier_order_no or "", o.order_no)
    if not result.get("success"):
        o.order_response = str(result.get("raw_response") or result)
        db.commit()
        raise HTTPException(400, result.get("message") or "快递公司取消失败")
    o.status = "cancelled"
    o.order_response = str(result.get("raw_response") or result)
    db.commit()
    return {"ok": True, "message": result.get("message")}


@router.post("/{order_id}/sf-city-tip")
def add_sf_city_tip(order_id: int, req: SFCityTipReq, request: Request,
                    background_tasks: BackgroundTasks,
                    db: Session = Depends(get_db)):
    """Add a real SF City gratuity after rechecking that no rider accepted."""
    order = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not order:
        raise HTTPException(404, "Not found")
    _assert_order_mutable(order, request)
    if str(order.courier_type or "").upper() != "SF_CITY":
        raise HTTPException(400, "只有顺丰同城订单可以增加小费")
    if not order.courier_order_no:
        raise HTTPException(400, "同城订单号尚未生成，暂时不能增加小费")

    previous_status = order.status
    previous_rider = order.courier_picked_by
    previous_track = order.track_status
    try:
        _refresh_order_tracking(db, order, include_freight=False)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(502, f"无法核实当前骑士接单状态：{exc}") from exc

    lifecycle_event = sf_city_event_from_transition(
        order, previous_status, previous_rider, previous_track
    )
    if lifecycle_event:
        queue_sf_city_status_notification(
            background_tasks, db, order, lifecycle_event, order.track_status or ""
        )

    from utils.sf_city_status import is_waiting_for_rider
    if not is_waiting_for_rider(order):
        raise HTTPException(409, "骑士已经接单或订单状态已变化，不能再增加小费")

    from courier import get_courier_for_order
    courier = get_courier_for_order(order, db)
    result = courier.add_gratuity(
        order.courier_order_no, req.amount,
        f"{order.order_no}-tip-{uuid.uuid4().hex}",
    )
    if not result.get("success"):
        raise HTTPException(400, result.get("message") or "顺丰同城加小费失败")
    return {
        "ok": True,
        "message": result.get("message"),
        "gratuity_fee": result.get("gratuity_fee"),
        "total_gratuity_fee": result.get("total_gratuity_fee"),
    }


def _to_dict(o, latest_track=None):
    if isinstance(latest_track, dict):
        latest_status = latest_track.get("status") or ""
        latest_detail = latest_track.get("detail") or ""
    else:
        latest_status = getattr(latest_track, "track_status", "") or ""
        latest_detail = getattr(latest_track, "track_detail", "") or ""
    displayed_track_status = preferred_track_status(
        o.track_status or "", latest_status, latest_detail
    )
    display = order_status_display(
        o.status,
        f"{displayed_track_status} {latest_detail}".strip(),
        bool(o.courier_order_no),
        baseline_track_text=o.track_status or "",
    )
    failure = extract_order_failure(o.order_response) if o.status == "failed" else {"code": "", "message": ""}
    from config.settings import settings
    from utils.sf_city_status import is_waiting_for_rider
    city_wait_minutes = max(1, int(settings.sf_city_rider_wait_minutes or 30))
    city_deadline = (
        o.order_time + dt.timedelta(minutes=city_wait_minutes)
        if str(o.courier_type or "").upper() == "SF_CITY" and o.order_time else None
    )
    data = {"id": o.id, "order_no": o.order_no, "courier_type": o.courier_type,
            "express_type": o.express_type, "express_type_name": o.express_type_name,
            "courier_order_no": o.courier_order_no,
            "can_resubmit": order_can_resubmit(o),
            "can_delete": order_can_delete(o),
            "sender_name": o.sender_name, "sender_phone": o.sender_phone,
            "sender_company": o.sender_company,
            "sender_province": o.sender_province, "sender_city": o.sender_city,
            "sender_district": o.sender_district, "sender_address": o.sender_address,
            "receiver_name": o.receiver_name, "receiver_phone": o.receiver_phone,
            "receiver_company": o.receiver_company,
            "receiver_province": o.receiver_province, "receiver_city": o.receiver_city,
            "receiver_district": o.receiver_district, "receiver_address": o.receiver_address,
            "receiver_lng": getattr(o, "receiver_lng", None),
            "receiver_lat": getattr(o, "receiver_lat", None),
            "item_name": o.item_name,
            "item_weight": o.item_weight, "item_volume": o.item_volume,
            "item_value": getattr(o, "item_value", None),
            "insurance_amount": o.insurance or 0, "parcel_count": o.parcel_count or 1,
            "courier_order_nos": json.loads(o.courier_order_nos) if o.courier_order_nos else [],
            "item_count": o.item_count,
            "item_remark": o.item_remark, "shipping_reason": o.shipping_reason,
            "freight": o.freight, "status": o.status,
            "status_category": display["status_category"],
            "status_label": display["status_label"],
            "sf_city_waiting_for_rider": is_waiting_for_rider(o),
            "sf_city_wait_timeout_minutes": city_wait_minutes,
            "sf_city_wait_deadline": city_deadline.isoformat() if city_deadline else None,
            "pickup_time_mode": o.pickup_time_mode,
            "pickup_start_time": o.pickup_start_time.isoformat() if o.pickup_start_time else None,
            "pickup_end_time": o.pickup_end_time.isoformat() if o.pickup_end_time else None,
            "source": o.source, "source_id": o.source_id, "approver_id": o.approver_id,
            "approve_time": o.approve_time.isoformat() if o.approve_time else None,
            "approve_remark": o.approve_remark,
            "approval_total_levels": o.approval_total_levels or 1,
            "approval_current_level": o.approval_current_level or 1,
            "approval_level1_required": o.approval_level1_required is not False,
            "approval_level1_user_id": o.approval_level1_user_id,
            "approval_level1_time": o.approval_level1_time.isoformat() if o.approval_level1_time else None,
            "approval_level1_remark": o.approval_level1_remark,
            "approval_level2_user_id": o.approval_level2_user_id,
            "approval_level2_time": o.approval_level2_time.isoformat() if o.approval_level2_time else None,
            "approval_level2_remark": o.approval_level2_remark,
            "order_time": o.order_time.isoformat() if o.order_time else None,
            "courier_picked_by": o.courier_picked_by,
            "courier_picked_by_phone": o.courier_picked_by_phone,
            "estimated_delivery_time": o.estimated_delivery_time.isoformat() if o.estimated_delivery_time else None,
            "route_code": o.route_code,
            "actual_freight": o.actual_freight,
            "actual_fee_detail": json.loads(o.actual_fee_detail) if o.actual_fee_detail else None,
            "cost_type": o.cost_type, "dept_code": o.dept_code, "dept_name": o.dept_name,
            "project_code": o.project_code, "project_name": o.project_name,
            "track_status": displayed_track_status,
            "track_update_time": o.track_update_time.isoformat() if o.track_update_time else None,
            "dispatch_error_code": failure["code"] or None,
            "dispatch_error_message": failure["message"] or None,
            "created_by": o.created_by,
            "created_at": o.created_at.isoformat() if o.created_at else None}
    return data

def _update_courier_assignment(order, track):
    """Persist assignment data when the carrier exposes it in a track event."""
    courier_info = track.get("courier") or {}
    name = str(courier_info.get("name") or "").strip()
    phone = str(courier_info.get("phone") or "").strip()
    if str(getattr(order, "courier_type", "") or "").upper() == "SF_CITY":
        from utils.sf_city_status import is_real_rider_name
        existing_name = str(getattr(order, "courier_picked_by", None) or "").strip()
        if not is_real_rider_name(name):
            # Clear an old placeholder and its false notification claim so a
            # later real assignment can still notify the applicant exactly once.
            if not is_real_rider_name(getattr(order, "courier_picked_by", None)):
                order.courier_picked_by = None
                order.courier_picked_by_phone = None
                clear_sf_city_notification(order, "rider_assigned")
            return
        if existing_name and not is_real_rider_name(existing_name):
            clear_sf_city_notification(order, "rider_assigned")
    if name:
        order.courier_picked_by = name
    if phone:
        order.courier_picked_by_phone = phone
