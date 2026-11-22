"""将数据库订单转换为快递公司的下单参数，并统一正式下单后的状态写入。"""
import datetime as dt
import json
from typing import Optional

from database import models


PICKUP_SLOT_START_HOURS = (8, 10, 12, 14, 16, 18)


def refresh_expired_pickup_window(order, now=None) -> bool:
    """审批时预约已过期，则顺延到最近一个尚未开始的两小时时段。"""
    now = now or dt.datetime.now()
    if order.pickup_time_mode != "appointment" or not order.pickup_start_time:
        return False
    if order.pickup_start_time > now:
        return False

    base_date = now.date()
    for day_offset in range(8):
        target_date = base_date + dt.timedelta(days=day_offset)
        for start_hour in PICKUP_SLOT_START_HOURS:
            start = dt.datetime.combine(target_date, dt.time(start_hour, 0))
            if start > now:
                order.pickup_start_time = start
                order.pickup_end_time = start + dt.timedelta(hours=2)
                return True
    return False


def _system_value(db, key: str, region_code: str = "xiamen_other") -> str:
    from utils.regions import get_config
    return get_config(db, region_code, key, "")


def build_order_data(order, db) -> dict:
    """两家适配器共用同一份业务订单，避免单单/批量字段不一致。"""
    refresh_expired_pickup_window(order)
    value = {
        "order_no": order.order_no,
        "sender_name": order.sender_name,
        "sender_phone": order.sender_phone,
        "sender_company": order.sender_company,
        "sender_province": order.sender_province,
        "sender_city": order.sender_city,
        "sender_district": order.sender_district,
        "sender_address": order.sender_address,
        "receiver_name": order.receiver_name,
        "receiver_phone": order.receiver_phone,
        "receiver_company": order.receiver_company,
        "receiver_province": order.receiver_province,
        "receiver_city": order.receiver_city,
        "receiver_district": order.receiver_district,
        "receiver_address": order.receiver_address,
        "receiver_lng": getattr(order, "receiver_lng", None),
        "receiver_lat": getattr(order, "receiver_lat", None),
        "item_name": order.item_name,
        "item_weight": order.item_weight,
        "item_value": getattr(order, "item_value", None),
        "item_volume": order.item_volume,
        "insurance_amount": order.insurance or 0,
        "parcel_count": order.parcel_count or 1,
        "item_count": order.item_count,
        "item_remark": order.item_remark,
        "express_type": order.express_type,
        "express_type_name": order.express_type_name,
        "pickup_time_mode": order.pickup_time_mode or "immediate",
        "pickup_start_time": order.pickup_start_time.strftime("%Y-%m-%d %H:%M:%S") if order.pickup_start_time else None,
        "pickup_end_time": order.pickup_end_time.strftime("%Y-%m-%d %H:%M:%S") if order.pickup_end_time else None,
    }
    if order.courier_type == "SF":
        value["express_type_id"] = int(order.express_type) if str(order.express_type or "").isdigit() else 2
        monthly = str(getattr(order, "billing_account_code", None) or "").strip() or _system_value(db, "sf_monthly_card", order.region_code)
        if monthly:
            value["monthly_card"] = monthly
    elif order.courier_type == "DB":
        customer_code = str(getattr(order, "billing_account_code", None) or "").strip() or _system_value(db, "db_customer_code", order.region_code)
        if customer_code:
            value["customer_code"] = customer_code
    return value


def parse_delivery(value) -> Optional[dt.datetime]:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None) if value.tzinfo is None else value.astimezone(
            dt.timezone(dt.timedelta(hours=8))
        ).replace(tzinfo=None)
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return dt.datetime.fromtimestamp(
            timestamp, tz=dt.timezone.utc
        ).astimezone(dt.timezone(dt.timedelta(hours=8))).replace(tzinfo=None)
    normalized = str(value).replace("T", " ").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(normalized[:19], fmt)
        except ValueError:
            continue
    return None


def dispatch_order(order, db) -> dict:
    """调用承运商正式下单接口并持久化结果。

    最终一级审批通过后使用此函数，统一处理运单号、
    费用、时效和轨迹的写入方式不一致。
    """
    carrier_message = ""
    error_code = ""
    from config.settings import settings
    if settings.preview_safe_mode:
        courier = str(order.courier_type or "DEMO").upper()
        waybill = f"DEMO-{courier}-{int(order.id):08d}"
        order.status = "ordered"
        order.courier_order_no = waybill
        order.courier_order_nos = json.dumps([waybill], ensure_ascii=False)
        order.order_time = dt.datetime.now()
        order.estimated_delivery_time = dt.datetime.now() + dt.timedelta(
            minutes=90 if courier == "SF_CITY" else 24 * 60
        )
        if courier == "SF_CITY" and order.freight is not None:
            order.actual_freight = order.freight
        order.order_response = json.dumps(
            {"preview": True, "message": "公开演示环境模拟下单成功"},
            ensure_ascii=False,
        )
        db.commit()
        carrier_message = "公开演示环境模拟下单成功"
    else:
        try:
            from courier import get_courier_for_order

            result = get_courier_for_order(order, db).create_order(build_order_data(order, db))
        except Exception as exc:
            order.status = "failed"
            order.order_response = str(exc)
            carrier_message = str(exc) or "承运商正式下单异常"
            error_code = "LOCAL_EXCEPTION"
        else:
            order.order_response = json.dumps(
                result.get("raw_response") or result, ensure_ascii=False, default=str
            )
            if result.get("success"):
                order.status = "ordered"
                order.courier_order_no = result.get("courier_order_no") or None
                order.courier_order_nos = json.dumps(
                    result.get("courier_order_nos")
                    or ([order.courier_order_no] if order.courier_order_no else []),
                    ensure_ascii=False,
                )
                if result.get("freight") is not None:
                    order.freight = result.get("freight")
                    if str(order.courier_type or "").upper() == "SF_CITY":
                        # 同城正式下单返回的是本次配送订单金额，可直接作为已确认金额展示；
                        # 后续刷新仍会以状态查询接口返回的最新金额覆盖（含小费等变化）。
                        order.actual_freight = result.get("freight")
                order.order_time = dt.datetime.now()
                order.route_code = result.get("dest_code")
                try:
                    order.estimated_delivery_time = parse_delivery(
                        result.get("estimated_delivery")
                    )
                except (TypeError, ValueError, OverflowError, OSError) as exc:
                    # The carrier order already exists.  A non-critical local field
                    # must never turn a successful real order into a failed order.
                    carrier_message = f"下单成功；预计送达时间暂未解析：{exc}"
                    error_code = "LOCAL_POST_PROCESSING_WARNING"
            else:
                order.status = "failed"
                carrier_message = str(result.get("message") or "承运商正式下单失败")
                error_code = str(result.get("error_code") or "")
    db.commit()

    if order.status == "ordered":
        is_sf_city = str(order.courier_type or "").upper() == "SF_CITY"
        initial_track_status = "寻找骑手中" if is_sf_city else "已下单"
        initial_track_detail = (
            "下单成功，正在查找骑士"
            if is_sf_city
            else (f"运单号 {order.courier_order_no}，等待揽收"
                  if order.courier_order_no else "快递公司已受理，等待生成运单号")
        )
        exists = db.query(models.CourierTrack).filter(
            models.CourierTrack.order_id == order.id,
            models.CourierTrack.track_status == initial_track_status,
        ).first()
        if not exists:
            db.add(models.CourierTrack(
                order_id=order.id,
                courier_order_no=order.courier_order_no,
                track_time=dt.datetime.now(),
                track_status=initial_track_status,
                track_detail=initial_track_detail,
            ))
            if is_sf_city:
                order.track_status = initial_track_status
                order.track_update_time = dt.datetime.now()
            db.commit()

    return {
        "ok": order.status == "ordered",
        "id": order.id,
        "order_no": order.order_no,
        "status": order.status,
        "courier_order_no": order.courier_order_no,
        "courier_order_nos": json.loads(order.courier_order_nos) if order.courier_order_nos else [],
        "estimated_delivery": order.estimated_delivery_time.isoformat() if order.estimated_delivery_time else None,
        "freight": order.freight,
        "error_code": error_code or None,
        "message": carrier_message or ("下单成功" if order.status == "ordered" else "承运商正式下单失败"),
    }
