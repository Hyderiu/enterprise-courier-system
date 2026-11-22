"""Normalize SF City rider fields returned by status and callback APIs."""
from __future__ import annotations


RIDER_PLACEHOLDER_PHRASES = (
    "暂未有骑士", "暂无骑士", "未有骑士", "无骑士", "骑士未接单",
    "骑手未接单", "待骑士接单", "待骑手接单", "正在寻找骑士",
    "寻找骑士中", "寻找骑手中", "正在匹配骑士", "待分配骑士",
)


def is_real_rider_name(value) -> bool:
    text = "".join(str(value or "").split())
    return bool(text) and not any(phrase in text for phrase in RIDER_PLACEHOLDER_PHRASES)


def is_waiting_for_rider(order) -> bool:
    """True only while a created SF City order has no accepted rider."""
    if str(getattr(order, "courier_type", "") or "").upper() != "SF_CITY":
        return False
    if not str(getattr(order, "courier_order_no", "") or "").strip():
        return False
    if str(getattr(order, "status", "") or "").lower() != "ordered":
        return False
    if is_real_rider_name(getattr(order, "courier_picked_by", None)):
        return False
    track = str(getattr(order, "track_status", "") or "")
    return not any(word in track for word in (
        "骑士已接单", "骑手已接单", "骑士已到店", "骑手已到店",
        "配送中", "已取货", "已送达", "已完成", "已取消",
    ))
