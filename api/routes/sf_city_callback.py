"""顺丰同城开放平台统一回调入口。

顺丰同城允许配送状态、完单、取消、异常等事件共用同一个 URL。
回调只在同时匹配本地订单号与顺丰同城订单号时修改订单，未知或用于
平台 URL 连通性校验的请求会安全确认但不会写业务数据。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from config.settings import settings
from database import models
from database.connection import get_db
from utils.order_status import apply_latest_track_state
from utils.approval_notifications import (
    clear_sf_city_notification, queue_sf_city_status_notification,
)
from utils.sf_city_status import is_real_rider_name


router = APIRouter()
log = logging.getLogger("courier.sf_city_callback")

SUCCESS_REPLY = {"error_code": 0, "error_msg": "success"}

# 顺丰同城状态：1 创建、2 取消、10 骑士接单、12 到店、15 已取货配送中、17 完成。
STATUS_MAP = {
    1: ("寻找骑手中", "pending_pickup"),
    2: ("顺丰同城订单已取消", "cancelled"),
    10: ("骑士已接单", "pending_pickup"),
    12: ("骑士已到店", "pending_pickup"),
    15: ("顺丰同城配送中", "delivering"),
    17: ("顺丰同城配送完成", "delivered"),
}

NOTIFICATION_EVENT_BY_STATUS = {
    1: "searching", 2: "cancelled", 10: "rider_assigned", 17: "delivered",
}


def _success() -> JSONResponse:
    return JSONResponse(SUCCESS_REPLY)


def _first(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _event_payload(body: Any) -> dict[str, Any]:
    """Unwrap common callback envelopes while preserving plain callback JSON."""
    if not isinstance(body, dict):
        return {}
    for key in ("data", "result", "msg_data", "msgData"):
        nested = body.get(key)
        if isinstance(nested, str):
            try:
                nested = json.loads(nested)
            except (TypeError, ValueError):
                nested = None
        if isinstance(nested, dict) and any(
            name in nested
            for name in ("order_status", "sf_order_id", "shop_order_id", "user_order_id", "url_index")
        ):
            return nested
    return body


async def _read_payload(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        return _event_payload(json.loads(raw.decode("utf-8-sig")))
    except (UnicodeDecodeError, ValueError, TypeError):
        pass
    try:
        form = await request.form()
    except Exception:
        return {}
    values = {str(key): value for key, value in form.items()}
    for key in ("data", "content", "payload"):
        value = values.get(key)
        if value:
            try:
                return _event_payload(json.loads(str(value)))
            except (TypeError, ValueError):
                continue
    return _event_payload(values)


def _verify_optional_token(request: Request) -> None:
    """Allow a deployment-specific callback token without requiring unsupported signing."""
    expected = str(settings.sf_city_callback_token or "").strip()
    if not expected:
        return
    supplied = (
        request.query_params.get("token")
        or request.headers.get("X-SF-City-Callback-Token")
        or ""
    )
    if not hmac.compare_digest(str(supplied), expected):
        raise HTTPException(status_code=401, detail="顺丰同城回调令牌校验失败")


def _event_time(payload: dict[str, Any]) -> dt.datetime:
    value = payload.get("push_time") or payload.get("timestamp")
    if value is None:
        return dt.datetime.now()
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return dt.datetime.fromtimestamp(number)
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return dt.datetime.now()


def _status(payload: dict[str, Any]) -> tuple[str, str]:
    status_code = payload.get("order_status")
    try:
        status_code = int(status_code)
    except (TypeError, ValueError):
        status_code = None
    label, category = STATUS_MAP.get(status_code, ("", ""))
    description = _first(payload, "status_desc", "status_description", "description", "remark")
    url_index = _first(payload, "url_index", "event_type", "callback_type").lower()
    if "exception" in url_index or "abnormal" in url_index:
        category = "exception"
        label = description or "顺丰同城配送异常"
    elif "cancel" in url_index:
        category = "cancelled"
        label = description or "顺丰同城订单已取消"
    elif "complete" in url_index or "finish" in url_index:
        category = "delivered"
        label = description or "顺丰同城配送完成"
    return description or label or "顺丰同城状态更新", category


def _find_order(db: Session, payload: dict[str, Any]):
    local_order_no = _first(
        payload, "user_order_id", "shop_order_id", "out_order_id", "order_no"
    )
    sf_order_no = _first(
        payload, "sf_order_id", "delivery_order_id", "waybill_id", "order_id"
    )
    # 同时匹配两个由不同系统生成的编号，避免伪造单个可猜编号修改订单。
    if local_order_no and sf_order_no:
        return db.query(models.Order).filter(
            models.Order.order_no == local_order_no,
            models.Order.courier_order_no == sf_order_no,
        ).first()
    return None


@router.get("/sf-city/callback")
def sf_city_callback_health(request: Request):
    """供开放平台保存 URL 时进行公网连通性校验。"""
    _verify_optional_token(request)
    return _success()


@router.post("/sf-city/callback")
async def sf_city_callback(
    request: Request, background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """接收顺丰同城全部回调类型；所有类型可在后台填写本 URL。"""
    _verify_optional_token(request)
    payload = await _read_payload(request)
    if not payload:
        # 开放平台可能用空请求检查 URL；回调 SDK 规定成功响应为此结构。
        return _success()

    order = _find_order(db, payload)
    if not order:
        log.info(
            "确认未匹配的顺丰同城回调 local_order=%s sf_order=%s event=%s",
            _first(payload, "user_order_id", "shop_order_id", "out_order_id", "order_no"),
            _first(payload, "sf_order_id", "delivery_order_id", "waybill_id", "order_id"),
            _first(payload, "url_index", "event_type", "callback_type"),
        )
        return _success()

    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    marker = f"[sfcity:{fingerprint}]"
    duplicate = db.query(models.CourierTrack).filter(
        models.CourierTrack.order_id == order.id,
        models.CourierTrack.track_detail.contains(marker),
    ).first()
    if duplicate:
        return _success()

    label, category = _status(payload)
    try:
        status_code = int(payload.get("order_status"))
    except (TypeError, ValueError):
        status_code = None
    rider_name = _first(payload, "rider_name", "operator_name", "courier_name")
    rider_phone = _first(payload, "rider_phone", "operator_phone", "courier_phone")
    detail_parts = [marker, label]
    if rider_name:
        detail_parts.append(f"骑士:{rider_name}")
    if rider_phone:
        detail_parts.append(f"电话:{rider_phone}")

    try:
        db.add(models.CourierTrack(
            order_id=order.id,
            courier_order_no=order.courier_order_no,
            track_time=_event_time(payload),
            track_status=label,
            track_detail=" ".join(detail_parts),
        ))
        order.track_update_time = dt.datetime.now()
        apply_latest_track_state(order, {
            "status": label,
            "detail": _first(payload, "status_desc", "description", "remark"),
            "category": category,
        })
        if rider_name and is_real_rider_name(rider_name):
            existing_rider = str(order.courier_picked_by or "").strip()
            if existing_rider and not is_real_rider_name(existing_rider):
                clear_sf_city_notification(order, "rider_assigned")
            order.courier_picked_by = rider_name
        if rider_phone and is_real_rider_name(rider_name):
            order.courier_picked_by_phone = rider_phone
        db.commit()
    except Exception:
        db.rollback()
        log.exception("处理顺丰同城回调失败")
        return JSONResponse(
            status_code=500,
            content={"error_code": 1, "error_msg": "internal error"},
        )
    lifecycle_event = NOTIFICATION_EVENT_BY_STATUS.get(status_code)
    if lifecycle_event:
        detail = _first(
            payload, "cancel_reason", "status_desc", "status_description",
            "description", "remark",
        )
        queue_sf_city_status_notification(
            background_tasks, db, order, lifecycle_event, detail
        )
    return _success()
