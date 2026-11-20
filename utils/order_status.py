"""One shared mapping from carrier text to product-facing order states."""


TRACK_CATEGORIES = {
    "pending_pickup", "picked", "transit", "delivering", "delivered",
    "exception", "cancelled",
}


TRACK_PROGRESS_RANK = {
    "pending_pickup": 0,
    "picked": 1,
    "transit": 2,
    "delivering": 3,
    "delivered": 4,
}


ORDER_STATUS_LABELS = {
    "pending": "待审核",
    "rejected": "审核失败",
    "pending_pickup": "待揽收",
    "picked": "已揽收",
    "transit": "在途",
    "delivering": "派送中",
    "delivered": "已签收",
    "exception": "异常单",
    "cancelled": "已取消",
}


SELF_PICKUP_WAITING_PHRASES = (
    "凭取货码取件", "凭取件码取件", "暂存至丰巢", "暂存到丰巢",
    "存放至丰巢", "存放到丰巢", "已放入丰巢", "已投递至丰巢",
    "已投递到丰巢", "暂存至快递柜", "暂存到快递柜",
    "已放入快递柜", "已投递至快递柜", "已投递到快递柜",
    "暂存至自提柜", "暂存到自提柜", "等待取货码取件",
)


def is_waiting_recipient_pickup(text=""):
    """Recognize a parcel waiting in a locker, not a carrier pickup event."""
    normalized = str(text or "").replace(" ", "")
    return any(phrase in normalized for phrase in SELF_PICKUP_WAITING_PHRASES)


def classify_track_event(status_text="", detail=""):
    """Classify one carrier event, preferring its concise status over prose detail."""
    primary = str(status_text or "").strip()
    combined = f"{primary} {detail or ''}".strip()

    def classify(text):
        if not text:
            return None
        if any(word in text for word in (
            "订单已取消", "运单已取消", "已撤销订单", "撤单成功", "取消成功",
            "运单已作废", "订单已关闭", "已取消寄件", "取消寄件", "寄件已取消",
        )):
            return "cancelled"
        if any(word in text for word in (
            "异常", "失败", "拒收", "退回", "退件", "破损", "遗失", "丢失",
            "滞留", "无法派送", "派送不成功", "超区", "销毁",
        )):
            return "exception"
        # 德邦把存入丰巢/快递柜并生成取件码视为末端妥投。这里的
        # “已取件”不是承运商刚揽收，按用户确认的业务口径显示已签收。
        if is_waiting_recipient_pickup(text):
            return "delivered"
        if any(word in text for word in (
            "已签收", "签收成功", "妥投", "已送达", "完成签收", "配送完成",
            "派送完成", "投递完成", "代收",
        )):
            return "delivered"
        if any(word in text for word in (
            "派送中", "配送中", "派件中", "正在派送", "正在派件", "投递中",
            "安排派送", "开始派送",
        )):
            return "delivering"
        if any(word in text for word in (
            "在途", "运输中", "运送中", "运输途中", "配送途中", "转运", "中转",
            "发往", "已发出", "已发车", "发车", "到达网点", "到达站点",
            "到达分拨", "到达配送站", "离开网点", "离开站点", "离开分拨",
            "快件已到达", "分拣中", "分拣完成", "分拨处理中", "装车", "卸车", "航班",
        )):
            return "transit"
        if any(word in text for word in (
            "已揽收", "揽收成功", "收取快件", "已取件", "取件成功", "已收件",
            "收件成功", "已揽件", "揽件成功", "揽件完成", "已取货", "已收货",
        )):
            return "picked"
        if any(word in text for word in (
            "待揽收", "等待揽收", "待取件", "等待取件", "等待收取", "已下单",
            "下单成功", "预约成功", "收派员接单", "等待收派员", "上门取件",
            "取件员接单", "取件员已接单", "已安排取件", "寻找骑手中",
            "正在查找骑士", "等待骑士接单", "骑士已接单", "骑手已接单",
        )):
            return "pending_pickup"
        return None

    # Carrier status names such as secondaryStatusName are more precise than the
    # long remark and prevent an old word in the prose from overriding a new state.
    return classify(primary) or classify(combined)


def _prefer_advanced_progress(current, candidate):
    """Keep carrier progress monotonic while still allowing terminal states."""
    if not current:
        return candidate
    if not candidate:
        return current
    if current in TRACK_PROGRESS_RANK and candidate in TRACK_PROGRESS_RANK:
        return max((current, candidate), key=TRACK_PROGRESS_RANK.__getitem__)
    return candidate


def preferred_track_status(current_status="", candidate_status="", candidate_detail=""):
    """Return carrier wording without replacing a later stage by an older one."""
    current_category = classify_track_event(current_status)
    candidate_category = classify_track_event(candidate_status, candidate_detail)
    if (
        current_category in TRACK_PROGRESS_RANK
        and candidate_category in TRACK_PROGRESS_RANK
        and TRACK_PROGRESS_RANK[candidate_category] < TRACK_PROGRESS_RANK[current_category]
    ):
        return str(current_status or "")
    return str(candidate_status or current_status or "")


def classify_order_status(status, track_text="", has_waybill=False, baseline_track_text=""):
    status = str(status or "").lower()
    track = str(track_text or "")
    if status == "cancelled":
        return "cancelled"
    if status == "rejected":
        return "rejected"
    if status == "failed":
        return "exception"
    if status == "delivered":
        return "delivered"
    event_category = classify_track_event(track)
    baseline_category = classify_track_event(baseline_track_text)
    event_category = _prefer_advanced_progress(baseline_category, event_category)
    if event_category:
        return event_category
    if status == "pending":
        return "pending"
    # Historical rows use ``success`` for a shipment that has already entered
    # carrier transport. Falling that value back to pending pickup caused the
    # same order to jump from “在途” to “待揽收” when carrier wording was empty.
    if status == "success":
        return "transit"
    return "pending_pickup" if status in {"approved", "ordered"} or has_waybill else "pending"



def order_status_display(status, track_text="", has_waybill=False, baseline_track_text=""):
    """Return the canonical category and user-facing label for every surface."""
    category = classify_order_status(
        status, track_text, has_waybill, baseline_track_text=baseline_track_text
    )
    label = ORDER_STATUS_LABELS.get(category, str(category or "待处理"))
    if str(status or "").lower() == "failed" and not has_waybill:
        label = "下单失败"
    elif category == "exception" and has_waybill:
        combined_track = f"{track_text or ''} {baseline_track_text or ''}"
        if "拒收" in combined_track:
            label = "客户拒收"
        elif any(word in combined_track for word in ("退回", "退件")):
            label = "退回中"
    return {
        "status_category": category,
        "status_label": label,
    }

def apply_latest_track_state(order, track):
    """Update the order summary fields from the latest carrier event."""
    status = str((track or {}).get("status") or "").strip()
    detail = str((track or {}).get("detail") or "").strip()
    category = str((track or {}).get("category") or "").strip()
    if category not in TRACK_CATEGORIES:
        category = classify_track_event(status, detail) or classify_order_status(
            order.status, f"{status} {detail}", bool(order.courier_order_no)
        )
    current_category = classify_track_event(order.track_status or "")
    if (
        current_category in TRACK_PROGRESS_RANK
        and category in TRACK_PROGRESS_RANK
        and TRACK_PROGRESS_RANK[category] < TRACK_PROGRESS_RANK[current_category]
    ):
        return current_category
    if status:
        order.track_status = status
    if (
        category == "pending_pickup"
        and str(getattr(order, "courier_type", "") or "").upper() == "SF_CITY"
        and bool(getattr(order, "courier_order_no", None))
        and str(getattr(order, "status", "") or "").lower() == "failed"
    ):
        # A real SF City order may have been created before a local
        # post-processing error marked the row failed.  A successful carrier
        # lookup showing that it is still waiting for a rider is authoritative.
        order.status = "ordered"
    if category == "exception":
        order.status = "failed"
    elif category == "delivered":
        order.status = "delivered"
    elif category in {"picked", "transit", "delivering"}:
        order.status = "success"
    elif category == "cancelled":
        order.status = "cancelled"
    return category
