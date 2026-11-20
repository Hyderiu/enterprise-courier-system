from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import models
from database.connection import get_db
from api.order_dispatch import dispatch_order
from utils.approval_policy import (
    actor_id_from_request, approve_level, assert_assigned_approver,
    ensure_approval_snapshot, reject_level,
)
from utils.approval_notifications import (
    queue_approval_notification, queue_approval_progress_notification,
    queue_approval_result_notification, queue_dispatch_notification,
    queue_sf_city_status_notification,
)

router = APIRouter()


def _courier_name(courier_type: str) -> str:
    return {"SF": "顺丰", "SF_CITY": "顺丰同城", "DB": "德邦", "JDL": "京东物流", "JD": "京东物流"}.get(
        str(courier_type or "").upper(), "快递公司"
    )


class ApproveReq(BaseModel):
    remark: Optional[str] = None


class RejectReq(BaseModel):
    remark: Optional[str] = None


def _preview_admin_requested(request: Request) -> bool:
    headers = getattr(request, "headers", {}) or {}
    return (
        not bool(getattr(request.state, "auth_enabled", False))
        and str(headers.get("X-Preview-Role") or "").lower() == "admin"
    )


def _assert_admin(request: Request, db: Session):
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


def _finalize_approved_order(order, remark, request, background_tasks, db):
    order.status = "approved"
    db.commit()
    is_sf_city = str(order.courier_type or "").upper() == "SF_CITY"
    notification = (
        {"queued": False, "reason": "同城订单将在正式下单后按实际状态提醒"}
        if is_sf_city else queue_approval_result_notification(
            background_tasks, db, order, True, remark or "", "order"
        )
    )
    result = dispatch_order(order, db)
    dispatch_notification = (
        {"queued": False, "reason": "顺丰同城下单结果由同城状态提醒通知申请人"}
        if is_sf_city else queue_dispatch_notification(
            background_tasks, db, order, result, "order"
        )
    )
    dispatch_ok = bool(result.get("ok"))
    dispatch_message = str(result.get("message") or "承运商正式下单失败")
    error_code = str(result.get("error_code") or "").strip()
    if error_code:
        dispatch_message = f"{dispatch_message}（错误码：{error_code}）"
    applicant_status_notification = None
    if is_sf_city:
        applicant_status_notification = queue_sf_city_status_notification(
            background_tasks, db, order,
            "searching" if dispatch_ok else "dispatch_failed",
            "" if dispatch_ok else dispatch_message,
        )
    result.update({
        "ok": True,
        "approval_completed": True,
        "dispatch_ok": dispatch_ok,
        "current_level": order.approval_total_levels,
        "notification": notification,
        "dispatch_notification": dispatch_notification,
        "applicant_status_notification": applicant_status_notification,
        "message": (
            ("审批已通过，顺丰同城下单成功，正在查找骑士"
             if is_sf_city else f"审批已通过，{_courier_name(order.courier_type)}下单成功")
            if dispatch_ok
            else f"审批已通过，但{_courier_name(order.courier_type)}下单失败：{dispatch_message}"
        ),
    })
    return result


@router.post("/{order_id}/approve")
def approve(order_id: int, req: ApproveReq, request: Request,
            background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    order = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not order:
        raise HTTPException(404, "Not found")
    if order.status != "pending":
        raise HTTPException(400, f"status={order.status}, cannot approve")

    ensure_approval_snapshot(order, db)
    actor_id = actor_id_from_request(request)
    assert_assigned_approver(
        order, actor_id, bool(getattr(request.state, "auth_enabled", False))
    )
    completed = approve_level(order, actor_id, req.remark or "")
    if not completed:
        db.commit()
        notification = queue_approval_notification(background_tasks, db, order, "order")
        applicant_notification = queue_approval_progress_notification(
            background_tasks, db, order, "level1_approved", "order"
        )
        return {
            "ok": True,
            "approval_completed": False,
            "current_level": order.approval_current_level,
            "total_levels": order.approval_total_levels,
            "message": "一级审批已通过，等待二级审批",
            "notification": notification,
            "applicant_notification": applicant_notification,
        }
    return _finalize_approved_order(
        order, req.remark or "", request, background_tasks, db
    )


@router.post("/{order_id}/skip/{level}")
def skip_level(order_id: int, level: int, req: ApproveReq, request: Request,
               background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    admin = _assert_admin(request, db)
    if level not in (1, 2):
        raise HTTPException(400, "审批层级只能是一级或二级")
    order = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not order:
        raise HTTPException(404, "Not found")
    if order.status != "pending":
        raise HTTPException(400, "当前单据已不在待审批状态")
    ensure_approval_snapshot(order, db)
    current = int(order.approval_current_level or 1)
    total = int(order.approval_total_levels or 1)
    if level > total:
        raise HTTPException(400, f"当前审批流没有第{level}级审批")
    if current != level:
        raise HTTPException(400, f"当前正在等待第{current}级审批，不能跳过第{level}级")
    actor_id = int(admin["id"])
    remark = _skip_remark(level, str(admin.get("name") or "管理员"), req.remark or "")
    setattr(order, f"approval_level{level}_user_id", actor_id)
    completed = approve_level(order, actor_id, remark)
    if not completed:
        db.commit()
        notification = queue_approval_notification(background_tasks, db, order, "order")
        applicant_notification = queue_approval_progress_notification(
            background_tasks, db, order, "level1_approved", "order"
        )
        return {
            "ok": True, "approval_completed": False,
            "current_level": order.approval_current_level,
            "total_levels": order.approval_total_levels,
            "message": "已跳过一级审批，等待二级审批",
            "notification": notification,
            "applicant_notification": applicant_notification,
        }
    return _finalize_approved_order(order, remark, request, background_tasks, db)


@router.post("/{order_id}/reject")
def reject(order_id: int, req: RejectReq, request: Request,
           background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    order = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not order:
        raise HTTPException(404, "Not found")
    if order.status != "pending":
        raise HTTPException(400, f"status={order.status}, cannot reject")
    ensure_approval_snapshot(order, db)
    actor_id = actor_id_from_request(request)
    assert_assigned_approver(
        order, actor_id, bool(getattr(request.state, "auth_enabled", False))
    )
    reject_level(order, actor_id, req.remark or "")
    order.status = "rejected"
    db.commit()
    notification = queue_approval_result_notification(
        background_tasks, db, order, False, req.remark or "", "order"
    )
    return {"ok": True, "notification": notification}
