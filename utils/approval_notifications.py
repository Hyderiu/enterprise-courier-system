"""Best-effort DingTalk notifications for approvals and delivery milestones."""
import json
import logging
from urllib.parse import quote

from config.settings import settings
from database import models
from dingtalk import DingTalkClient
from utils.dingtalk_notification_auth import create_notification_login_token
from utils.approval_policy import get_approval_policy


logger = logging.getLogger(__name__)


def dingtalk_workbench_url(target_url):
    """Open a URL in DingTalk's large workbench container instead of its side panel."""
    separator = "&" if "?" in target_url else "?"
    workbench_target = f"{target_url}{separator}ddtab=true"
    return "dingtalk://dingtalkclient/page/link?url=" + quote(workbench_target, safe="")


def dingtalk_authenticated_url(target_path):
    """Start DingTalk SSO first, then return to the exact application record."""
    base_url = str(settings.public_base_url or "").strip().rstrip("/")
    if not base_url:
        return ""
    path = str(target_path or "/")
    if not path.startswith("/") or path.startswith("//"):
        path = "/"
    login_url = f"{base_url}/dingtalk-entry?next={quote(path, safe='')}"
    corp_id = str(settings.dingtalk_corp_id or "").strip()
    if corp_id and corp_id != "$CORPID$":
        login_url += "&corpid=" + quote(corp_id, safe="")
    return dingtalk_workbench_url(login_url)


def dingtalk_notification_url(user, target_path):
    """Create a recipient-bound direct login link for a private DingTalk work notice."""
    base_url = str(settings.public_base_url or "").strip().rstrip("/")
    if not base_url:
        return ""
    token = create_notification_login_token(
        user.id, user.dingtalk_userid, target_path
    )
    return dingtalk_workbench_url(
        f"{base_url}/dingtalk-notification-entry?token={quote(token, safe='')}"
    )


def _current_approver_id(record, record_type="order"):
    if record_type == "manual":
        return getattr(record, "approval_level2_user_id", None), 2
    level = int(getattr(record, "approval_current_level", None) or 1)
    return getattr(record, f"approval_level{level}_user_id", None), level


def _record_details(record, record_type):
    if record_type == "batch":
        return (
            getattr(record, "batch_no", None) or f"批次 {record.id}",
            "批量寄件申请",
            f"/orders?batch_id={record.id}",
            f"/admin/approvals?batch_id={record.id}",
        )
    if record_type == "manual":
        return (
            getattr(record, "waybill_no", None) or f"补充记录 {record.id}",
            "运单补充记录",
            f"/waybill-records?record_id={record.id}",
            f"/admin/approvals?manual_id={record.id}",
        )
    return (
        getattr(record, "order_no", None) or f"订单 {record.id}",
        "寄件申请",
        f"/orders?order_id={record.id}",
        f"/admin/approvals?order_id={record.id}",
    )


def build_approval_notification(db, record, record_type="order", reminder=False):
    """Resolve database-backed values before the background task starts."""
    base_url = str(settings.public_base_url or "").strip().rstrip("/")
    if not base_url:
        return None, "PUBLIC_BASE_URL 未配置"
    approver_id, level = _current_approver_id(record, record_type)
    if not approver_id:
        return None, f"第{level}级审批人未配置"
    approver = db.query(models.User).filter(models.User.id == approver_id).first()
    if not approver or not str(approver.dingtalk_userid or "").strip():
        return None, "审批人没有钉钉 userid"
    applicant = None
    if getattr(record, "created_by", None):
        applicant = db.query(models.User).filter(models.User.id == record.created_by).first()
    applicant_name = (applicant.name if applicant else None) or "员工"

    if record_type == "batch":
        count = int(getattr(record, "total_count", None) or 0)
        reference = getattr(record, "batch_no", None) or f"批次 {record.id}"
        title = f"{'审批催办' if reminder else '待审批'}：{applicant_name}的批量寄件申请"
        markdown = (
            f"### {'批量寄件申请仍在等待您审批' if reminder else '新的批量寄件申请待审批'}\n\n"
            f"- 申请人：{applicant_name}\n"
            f"- 批次：{reference}\n"
            f"- 收件数量：{count} 票\n"
            f"- 当前环节：第 {level} 级审批"
        )
        action_url = dingtalk_notification_url(approver,
            f"/admin/approvals?batch_id={record.id}"
        )
    elif record_type == "manual":
        reference = getattr(record, "waybill_no", None) or f"补充记录 {record.id}"
        title = f"{'审核催办' if reminder else '待审核'}：{applicant_name}的运单补充记录"
        markdown = (
            f"### {'运单补充记录仍在等待您审核' if reminder else '新的运单补充记录待审核'}\n\n"
            f"- 提交人：{applicant_name}\n"
            f"- 运单号：{reference}\n"
            f"- 当前环节：二级审核"
        )
        action_url = dingtalk_notification_url(
            approver, f"/admin/approvals?manual_id={record.id}"
        )
    else:
        reference = getattr(record, "order_no", None) or f"订单 {record.id}"
        receiver = getattr(record, "receiver_name", None) or "未填写"
        title = f"{'审批催办' if reminder else '待审批'}：{applicant_name}的寄件申请"
        markdown = (
            f"### {'寄件申请仍在等待您审批' if reminder else '新的寄件申请待审批'}\n\n"
            f"- 申请人：{applicant_name}\n"
            f"- 申请编号：{reference}\n"
            f"- 收件人：{receiver}\n"
            f"- 当前环节：第 {level} 级审批"
        )
        action_url = dingtalk_notification_url(approver,
            f"/admin/approvals?order_id={record.id}"
        )
    return {
        "userid": approver.dingtalk_userid,
        "title": title,
        "markdown": markdown,
        "action_title": "打开审批页面",
        "action_url": action_url,
    }, None


def build_approval_progress_notification(db, record, event="submitted", record_type="order"):
    """Notify the applicant after submission or an intermediate approval."""
    if not str(settings.public_base_url or "").strip():
        return None, "PUBLIC_BASE_URL 未配置"
    applicant_id = getattr(record, "created_by", None)
    applicant = db.query(models.User).filter(models.User.id == applicant_id).first() \
        if applicant_id else None
    if not applicant or not str(applicant.dingtalk_userid or "").strip():
        return None, "申请人没有钉钉 userid"
    approver_id, level = _current_approver_id(record, record_type)
    approver = db.query(models.User).filter(models.User.id == approver_id).first() \
        if approver_id else None
    approver_name = (approver.name if approver else None) or "尚未配置"
    reference, subject, target_path, _ = _record_details(record, record_type)
    stage_name = "二级审核" if record_type == "manual" else f"第 {level} 级审批"
    if event == "level1_approved":
        title = f"一级审批已通过：{reference}"
        heading = f"您的{subject}一级审批已通过"
    else:
        title = f"提交成功：{reference}"
        heading = f"您的{subject}已提交成功"
    return {
        "userid": applicant.dingtalk_userid,
        "title": title,
        "markdown": (
            f"### {heading}\n\n"
            f"- 申请编号：{reference}\n"
            f"- 当前阶段：{stage_name}\n"
            f"- 当前审批人：{approver_name}"
        ),
        "action_title": "查看申请进度",
        "action_url": dingtalk_notification_url(applicant, target_path),
    }, None


def build_approval_result_notification(db, record, approved, remark="", record_type="order"):
    """Build a decision notification for the employee who submitted the request."""
    base_url = str(settings.public_base_url or "").strip().rstrip("/")
    if not base_url:
        return None, "PUBLIC_BASE_URL 未配置"
    applicant_id = getattr(record, "created_by", None)
    applicant = db.query(models.User).filter(models.User.id == applicant_id).first() \
        if applicant_id else None
    if not applicant or not str(applicant.dingtalk_userid or "").strip():
        return None, "申请人没有钉钉 userid"
    approver_id = (
        getattr(record, "approval_level2_user_id", None)
        if record_type == "manual"
        else getattr(record, "approver_id", None)
    )
    approver = db.query(models.User).filter(models.User.id == approver_id).first() \
        if approver_id else None
    approver_name = (approver.name if approver else None) or "审批人"
    decision = "已通过" if approved else "未通过"
    clean_remark = str(remark or "").strip()[:1000]
    opinion = clean_remark if clean_remark else "未填写"
    reference, subject, target_path, _ = _record_details(record, record_type)
    return {
        "userid": applicant.dingtalk_userid,
        "title": f"{subject}{decision}：{reference}",
        "markdown": (
            f"### 您的{subject}{decision}\n\n"
            f"- 申请编号：{reference}\n"
            f"- 审批结果：{decision}\n"
            f"- 审批人：{approver_name}\n"
            f"- 审批意见：{opinion}"
        ),
        "action_title": "查看申请和审批意见",
        "action_url": dingtalk_notification_url(applicant, target_path),
    }, None


def _courier_display_name(record):
    return {
        "SF": "顺丰速运",
        "SF_CITY": "顺丰同城急送",
        "DB": "德邦快递",
        "JDL": "京东物流",
        "JD": "京东物流",
    }.get(str(getattr(record, "courier_type", "") or "").upper(), "快递公司")


def build_dispatch_notification(db, record, dispatch_result, record_type="order"):
    """Tell the applicant whether the selected courier accepted the order."""
    applicant_id = getattr(record, "created_by", None)
    applicant = db.query(models.User).filter(models.User.id == applicant_id).first() \
        if applicant_id else None
    if not applicant or not str(applicant.dingtalk_userid or "").strip():
        return None, "申请人没有钉钉 userid"
    reference, _, target_path, _ = _record_details(record, record_type)
    courier_name = _courier_display_name(record)
    success = bool((dispatch_result or {}).get("ok"))
    if record_type == "batch":
        detail = (
            f"成功 {int((dispatch_result or {}).get('success') or 0)} 票，"
            f"失败 {int((dispatch_result or {}).get('failed') or 0)} 票"
        )
    else:
        waybill = getattr(record, "courier_order_no", None)
        detail = f"运单号：{waybill}" if success and waybill else str(
            (dispatch_result or {}).get("message") or f"{courier_name}未返回下单结果"
        )
    result_text = "下单成功" if success else "下单失败"
    heading = (
        f"{courier_name}批量下单{'完成' if success else '存在失败'}"
        if record_type == "batch" else f"{courier_name}{result_text}"
    )
    return {
        "userid": applicant.dingtalk_userid,
        "title": f"{heading}：{reference}",
        "markdown": (
            f"### {heading}\n\n"
            f"- 申请编号：{reference}\n"
            f"- 快递公司：{courier_name}\n"
            f"- 下单结果：{result_text}\n"
            f"- 结果明细：{detail}"
        ),
        "action_title": "查看订单详情" if record_type == "order" else "查看批次详情",
        "action_url": dingtalk_notification_url(applicant, target_path),
    }, None


SF_CITY_NOTIFICATION_EVENTS = {
    "searching": ("同城下单成功", "下单成功，正在查找骑士"),
    "rider_assigned": ("同城骑士已接单", "已找到骑士，正在前往取件"),
    "cancelled": ("同城订单已取消", "未找到骑士，订单已取消"),
    "delivered": ("同城订单已送达", "顺丰同城订单已送达"),
    "dispatch_failed": ("同城下单失败", "审批已通过，但顺丰同城下单失败"),
}


def _sf_city_events(record):
    try:
        value = json.loads(getattr(record, "sf_city_notification_events", None) or "[]")
    except (TypeError, ValueError):
        value = []
    return {str(item) for item in value if str(item) in SF_CITY_NOTIFICATION_EVENTS}


def sf_city_notification_sent(record, event):
    return str(event) in _sf_city_events(record)


def mark_sf_city_notification(record, event):
    events = _sf_city_events(record)
    events.add(str(event))
    record.sf_city_notification_events = json.dumps(sorted(events), ensure_ascii=False)


def clear_sf_city_notification(record, event):
    events = _sf_city_events(record)
    events.discard(str(event))
    record.sf_city_notification_events = json.dumps(sorted(events), ensure_ascii=False)


def build_sf_city_status_notification(db, record, event, detail=""):
    """Notify the submitting employee about one SF City delivery milestone."""
    if event not in SF_CITY_NOTIFICATION_EVENTS:
        return None, f"未知顺丰同城提醒事件：{event}"
    if not str(settings.public_base_url or "").strip():
        return None, "PUBLIC_BASE_URL 未配置"
    applicant_id = getattr(record, "created_by", None)
    applicant = db.query(models.User).filter(models.User.id == applicant_id).first() \
        if applicant_id else None
    if not applicant or not str(applicant.dingtalk_userid or "").strip():
        return None, "申请人没有钉钉 userid"

    title_text, heading = SF_CITY_NOTIFICATION_EVENTS[event]
    if event == "rider_assigned":
        from utils.sf_city_status import is_real_rider_name
        rider_name = getattr(record, "courier_picked_by", None)
        if rider_name and not is_real_rider_name(rider_name):
            return None, "顺丰同城尚未分配真实骑士"
    if event == "cancelled" and str(
        getattr(record, "courier_picked_by", None) or ""
    ).strip():
        # 状态码 2 也可能来自人工取消或接单后的取消。只有尚未分配
        # 骑士时才能明确描述为“未找到骑士”，避免向申请人误报原因。
        heading = "顺丰同城订单已取消"
    reference = getattr(record, "order_no", None) or f"订单 {record.id}"
    lines = [
        f"### {heading}", "", f"- 申请编号：{reference}",
    ]
    if event == "searching":
        lines.append(f"- 同城订单号：{getattr(record, 'courier_order_no', None) or '顺丰处理中'}")
        lines.append("- 当前状态：正在查找骑士")
        wait_minutes = max(1, int(settings.sf_city_rider_wait_minutes or 30))
        lines.append(f"- 等待规则：{wait_minutes} 分钟内仍无骑士接单，订单将自动取消")
        lines.append("- 提高接单概率：可进入订单详情增加小费")
    elif event == "rider_assigned":
        lines.append(f"- 骑士：{getattr(record, 'courier_picked_by', None) or '顺丰已安排'}")
        phone = str(getattr(record, "courier_picked_by_phone", None) or "").strip()
        if phone:
            lines.append(f"- 骑士电话：{phone}")
    elif event == "cancelled":
        lines.append("- 当前状态：已取消")
    elif event == "delivered":
        lines.append("- 当前状态：已送达")
    elif event == "dispatch_failed":
        lines.append("- 当前状态：下单失败")
    clean_detail = str(detail or "").strip()
    if clean_detail:
        lines.append(f"- 状态说明：{clean_detail[:300]}")
    return {
        "userid": applicant.dingtalk_userid,
        "title": f"{title_text}：{reference}",
        "markdown": "\n".join(lines),
        "action_title": "查看订单详情 / 加小费" if event == "searching" else "查看订单详情",
        "action_url": dingtalk_notification_url(
            applicant, f"/orders?order_id={record.id}"
        ),
    }, None


def queue_sf_city_status_notification(background_tasks, db, record, event, detail=""):
    """Queue one deduplicated lifecycle notice and persist the claimed event."""
    if sf_city_notification_sent(record, event):
        return {"queued": False, "duplicate": True}
    job, reason = build_sf_city_status_notification(db, record, event, detail)
    if not job:
        logger.warning("DingTalk SF City status notification skipped: %s", reason)
        return {"queued": False, "reason": reason}
    mark_sf_city_notification(record, event)
    db.commit()
    background_tasks.add_task(send_approval_notification, job)
    return {"queued": True}


def send_sf_city_status_notification_once(db, record, event, detail=""):
    """Send a deduplicated notice from the tracking scheduler fallback."""
    if sf_city_notification_sent(record, event):
        return {"sent": False, "duplicate": True}
    job, reason = build_sf_city_status_notification(db, record, event, detail)
    if not job:
        return {"sent": False, "reason": reason}
    result = send_approval_notification(job)
    if result.get("sent"):
        mark_sf_city_notification(record, event)
        db.commit()
    return result


def sf_city_event_from_transition(
    record, previous_status="", previous_rider="", previous_track_status=""
):
    """Resolve lifecycle milestones discovered by polling when a callback was missed."""
    if str(getattr(record, "courier_type", "") or "").upper() != "SF_CITY":
        return None
    current_status = str(getattr(record, "status", "") or "").lower()
    current_track = str(getattr(record, "track_status", "") or "")
    if current_status == "cancelled" and str(previous_status).lower() != "cancelled":
        return "cancelled"
    if current_status == "delivered" and str(previous_status).lower() != "delivered":
        return "delivered"
    from utils.sf_city_status import is_real_rider_name
    current_rider = str(getattr(record, "courier_picked_by", "") or "").strip()
    assigned_now = (
        is_real_rider_name(current_rider)
        and not is_real_rider_name(previous_rider)
    )
    track_assigned_now = (
        any(word in current_track for word in ("骑士已接单", "骑手已接单"))
        and not any(word in str(previous_track_status or "") for word in ("骑士已接单", "骑手已接单"))
    )
    if assigned_now or track_assigned_now:
        return "rider_assigned"
    return None


def build_feedback_notification(db, feedback):
    """Notify the configured level-two reviewer about one employee opinion."""
    if not str(settings.public_base_url or "").strip():
        return None, "PUBLIC_BASE_URL 未配置"
    reviewer_id = get_approval_policy(db).get("level2_user_id")
    if not reviewer_id:
        return None, "二级审核员未配置"
    reviewer = db.query(models.User).filter(models.User.id == reviewer_id).first()
    if not reviewer or not str(reviewer.dingtalk_userid or "").strip():
        return None, "二级审核员没有钉钉 userid"
    applicant = db.query(models.User).filter(
        models.User.id == getattr(feedback, "user_id", None)
    ).first()
    applicant_name = (applicant.name if applicant else None) or "员工"
    content = str(getattr(feedback, "content", "") or "").strip()
    preview = content[:180] + ("…" if len(content) > 180 else "")
    return {
        "userid": reviewer.dingtalk_userid,
        "title": f"待处理：{applicant_name}提交了新意见",
        "markdown": (
            "### 新的员工意见待处理\n\n"
            f"- 提交人：{applicant_name}\n"
            f"- 意见内容：{preview}\n"
            "- 状态：待处理"
        ),
        "action_title": "打开意见处理页面",
        "action_url": dingtalk_notification_url(
            reviewer, f"/admin/feedback?feedback_id={feedback.id}"
        ),
    }, None


def send_approval_notification(job):
    """Send without affecting the order transaction when DingTalk is unavailable."""
    if not job:
        return {"sent": False, "reason": "通知任务为空"}
    try:
        response = DingTalkClient().send_work_notification(**job)
        logger.info("DingTalk approval notification sent: task_id=%s", response.get("task_id"))
        return {"sent": True, "task_id": response.get("task_id")}
    except Exception as exc:
        logger.exception("DingTalk approval notification failed: %s", exc)
        return {"sent": False, "reason": str(exc)}


def queue_approval_notification(background_tasks, db, record, record_type="order", reminder=False):
    job, reason = build_approval_notification(db, record, record_type, reminder=reminder)
    if not job:
        logger.warning("DingTalk approval notification skipped: %s", reason)
        return {"queued": False, "reason": reason}
    background_tasks.add_task(send_approval_notification, job)
    return {"queued": True}


def _queue_job(background_tasks, job, reason, label):
    if not job:
        logger.warning("DingTalk %s notification skipped: %s", label, reason)
        return {"queued": False, "reason": reason}
    background_tasks.add_task(send_approval_notification, job)
    return {"queued": True}


def queue_approval_progress_notification(
    background_tasks, db, record, event="submitted", record_type="order"
):
    job, reason = build_approval_progress_notification(db, record, event, record_type)
    return _queue_job(background_tasks, job, reason, "approval progress")


def queue_dispatch_notification(
    background_tasks, db, record, dispatch_result, record_type="order"
):
    job, reason = build_dispatch_notification(
        db, record, dispatch_result, record_type
    )
    return _queue_job(background_tasks, job, reason, "dispatch result")


def queue_approval_result_notification(
    background_tasks, db, record, approved, remark="", record_type="order"
):
    job, reason = build_approval_result_notification(
        db, record, approved, remark, record_type
    )
    if not job:
        logger.warning("DingTalk approval result notification skipped: %s", reason)
        return {"queued": False, "reason": reason}
    background_tasks.add_task(send_approval_notification, job)
    return {"queued": True}


def queue_feedback_notification(background_tasks, db, feedback):
    job, reason = build_feedback_notification(db, feedback)
    if not job:
        logger.warning("DingTalk feedback notification skipped: %s", reason)
        return {"queued": False, "reason": reason}
    background_tasks.add_task(send_approval_notification, job)
    return {"queued": True}
