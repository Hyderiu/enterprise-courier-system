"""Employee and administrator APIs for shipments created outside this system."""
from __future__ import annotations

from datetime import datetime, timedelta
import re
from types import SimpleNamespace
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import models
from database.connection import get_db
from utils.billing_allocation import CARRIERS, REGION_NAMES, _waybill
from utils.billing_workbench import rematch_for_manual_record
from utils.department_paths import build_department_path_map
from utils.approval_policy import get_approval_policy
from utils.approval_notifications import (
    queue_approval_notification, queue_approval_progress_notification,
    queue_approval_result_notification,
)


router = APIRouter()


class ManualWaybillBody(BaseModel):
    region_code: str
    courier_type: str
    waybill_no: str
    sender_name: Optional[str] = None
    cost_type: str
    dept_code: Optional[str] = None
    project_code: Optional[str] = None
    created_by: Optional[int] = None


class ManualWaybillDecision(BaseModel):
    remark: Optional[str] = None


def _identity(request: Request):
    current = getattr(request.state, "current_user", None) or {}
    return current.get("id"), str(current.get("role") or "employee")


def _assert_mutable(record, request):
    user_id, role = _identity(request)
    if role == "admin" or not getattr(request.state, "auth_enabled", False):
        return
    if not user_id or int(user_id) != int(record.created_by):
        raise HTTPException(403, "只能修改或撤回自己提交的运单记录")


def _assigned_level2_user_id(db):
    reviewer_id = get_approval_policy(db).get("level2_user_id")
    if not reviewer_id:
        raise HTTPException(409, "二级审核员尚未配置，请先到审批流程配置中设置")
    reviewer = db.query(models.User).filter(
        models.User.id == reviewer_id, models.User.is_active.is_(True),
    ).first()
    if not reviewer:
        raise HTTPException(409, "二级审核员不存在或账号已停用")
    return reviewer.id


def _reset_for_submission(record, db):
    record.status = "pending_approval"
    record.approval_level2_user_id = _assigned_level2_user_id(db)
    record.approval_level2_time = None
    record.approval_level2_remark = None
    record.approval_stage_started_at = models.china_now()
    record.approval_last_reminded_at = None


def _assert_level2_reviewer(record, request):
    user_id, _ = _identity(request)
    if not getattr(request.state, "auth_enabled", False) and user_id is None:
        return
    if not user_id or int(user_id) != int(record.approval_level2_user_id or 0):
        raise HTTPException(403, "当前补充记录应由指定二级审核员处理")


def _validated_values(db, body, *, sender_name: str = ""):
    region = str(body.region_code or "").strip().lower()
    courier = str(body.courier_type or "").strip().upper()
    number = _waybill(body.waybill_no)
    sender = str(sender_name or body.sender_name or "").strip()
    cost_type = str(body.cost_type or "").strip().lower()
    if region not in REGION_NAMES:
        raise HTTPException(400, "请选择具体区域")
    if courier not in CARRIERS:
        raise HTTPException(400, "请选择顺丰、德邦或京东")
    if not number:
        raise HTTPException(400, "请填写运单号")
    if not sender:
        raise HTTPException(400, "无法读取提交员工姓名")
    if cost_type not in {"department", "project"}:
        raise HTTPException(400, "请选择部门费用或项目费用")
    values = {
        "region_code": region, "courier_type": courier, "waybill_no": number,
        "sender_name": sender, "cost_type": cost_type,
        "dept_code": None, "dept_name": None,
        "project_code": None, "project_name": None,
    }
    departments = db.query(models.Department).all()
    department_map = {str(item.dept_code): item for item in departments}
    paths = build_department_path_map(departments)
    if cost_type == "department":
        code = str(body.dept_code or "").strip()
        department = department_map.get(code)
        if not department or not department.enabled:
            raise HTTPException(400, "请选择当前 CRM 中有效的部门")
        values.update({
            "dept_code": code,
            "dept_name": paths.get(department.id, department.dept_name),
        })
    else:
        code = str(body.project_code or "").strip()
        project = db.query(models.Project).filter(
            models.Project.project_code == code, models.Project.enabled == True,
        ).first()
        if not project:
            raise HTTPException(400, "请选择当前 CRM 中有效的项目")
        values.update({
            "project_code": code, "project_name": project.project_name,
            "dept_code": project.dept_code, "dept_name": project.dept_name,
        })
    return values


def _record_dict(record, creator_name=""):
    return {
        "id": record.id, "region_code": record.region_code,
        "region_name": REGION_NAMES.get(record.region_code, record.region_code),
        "courier_type": record.courier_type,
        "carrier_name": CARRIERS.get(record.courier_type, record.courier_type),
        "waybill_no": record.waybill_no, "sender_name": record.sender_name,
        "cost_type": record.cost_type, "dept_code": record.dept_code,
        "dept_name": record.dept_name, "project_code": record.project_code,
        "project_name": record.project_name, "status": record.status,
        "status_name": {
            "pending_approval": "待二级审核", "approved": "审核通过",
            "rejected": "已驳回", "withdrawn": "已撤回",
        }.get(record.status, record.status),
        "approval_level2_user_id": record.approval_level2_user_id,
        "approval_level2_time": (
            record.approval_level2_time.isoformat(timespec="seconds")
            if record.approval_level2_time else None
        ),
        "approval_level2_remark": record.approval_level2_remark,
        "created_by": record.created_by, "created_by_name": creator_name,
        "created_at": record.created_at.isoformat(timespec="seconds") if record.created_at else None,
        "updated_at": record.updated_at.isoformat(timespec="seconds") if record.updated_at else None,
    }


@router.get("/manual-waybills")
@router.get("/admin/manual-waybills")
def list_manual_waybills(
    request: Request, region: str = "all", courier_type: str = "all",
    status: str = "all", start_date: str = "", end_date: str = "",
    month: str = "", query: str = "", db: Session = Depends(get_db),
):
    user_id, role = _identity(request)
    q = db.query(models.ManualWaybillRecord)
    if getattr(request.state, "auth_enabled", False) and role != "admin":
        if not user_id:
            raise HTTPException(401, "请先登录")
        q = q.filter(models.ManualWaybillRecord.created_by == int(user_id))
    if region != "all":
        q = q.filter(models.ManualWaybillRecord.region_code == region)
    courier = str(courier_type or "all").upper()
    if courier != "ALL":
        q = q.filter(models.ManualWaybillRecord.courier_type == courier)
    if status in {"pending_approval", "approved", "rejected", "withdrawn"}:
        q = q.filter(models.ManualWaybillRecord.status == status)
    if month:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(400, "月份格式必须为 YYYY-MM")
        month_start = datetime.strptime(month, "%Y-%m")
        month_end = (
            datetime(month_start.year + 1, 1, 1)
            if month_start.month == 12
            else datetime(month_start.year, month_start.month + 1, 1)
        )
        q = q.filter(
            models.ManualWaybillRecord.created_at >= month_start,
            models.ManualWaybillRecord.created_at < month_end,
        )
    try:
        if start_date:
            q = q.filter(models.ManualWaybillRecord.created_at >= datetime.strptime(start_date, "%Y-%m-%d"))
        if end_date:
            end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
            q = q.filter(models.ManualWaybillRecord.created_at < end)
    except ValueError as exc:
        raise HTTPException(400, "日期格式必须为 YYYY-MM-DD") from exc
    search = str(query or "").strip()
    if search:
        like = f"%{search}%"
        q = q.filter(or_(
            models.ManualWaybillRecord.waybill_no.like(like),
            models.ManualWaybillRecord.sender_name.like(like),
            models.ManualWaybillRecord.dept_name.like(like),
            models.ManualWaybillRecord.project_name.like(like),
        ))
    records = q.order_by(models.ManualWaybillRecord.created_at.desc()).limit(1000).all()
    user_ids = {item.created_by for item in records}
    creators = {
        user.id: user.name for user in db.query(models.User).filter(
            models.User.id.in_(user_ids or [-1]),
        ).all()
    }
    return {"ok": True, "items": [_record_dict(item, creators.get(item.created_by, "")) for item in records]}


@router.post("/manual-waybills")
@router.post("/admin/manual-waybills")
def create_manual_waybill(
    body: ManualWaybillBody, request: Request, db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None,
):
    background_tasks = background_tasks or BackgroundTasks()
    user_id, role = _identity(request)
    auth_enabled = bool(getattr(request.state, "auth_enabled", False))
    owner_id = body.created_by if role == "admin" or not auth_enabled else user_id
    if not owner_id and not auth_enabled:
        preview_owner = db.query(models.User).filter(
            models.User.is_active.isnot(False),
        ).order_by(models.User.id).first()
        owner_id = preview_owner.id if preview_owner else None
    owner = db.query(models.User).filter(models.User.id == owner_id).first() if owner_id else None
    if not owner:
        raise HTTPException(400, "无法确定提交员工")
    # 补充记录的提交员工由钉钉用户主键确定。管理员代员工新增时，
    # 页面不再重复填写寄件人姓名，直接使用所选员工的实名快照。
    automatic_sender = owner.name or ""
    values = _validated_values(db, body, sender_name=automatic_sender)
    duplicate = db.query(models.ManualWaybillRecord).filter(
        models.ManualWaybillRecord.region_code == values["region_code"],
        models.ManualWaybillRecord.courier_type == values["courier_type"],
        models.ManualWaybillRecord.waybill_no == values["waybill_no"],
        models.ManualWaybillRecord.status.in_(("pending_approval", "approved")),
    ).first()
    if duplicate:
        raise HTTPException(409, "该区域和快递下已存在相同运单号的有效补充记录")
    record = models.ManualWaybillRecord(**values, created_by=owner.id)
    _reset_for_submission(record, db)
    db.add(record)
    db.commit()
    db.refresh(record)
    notification = queue_approval_notification(
        background_tasks, db, record, "manual"
    )
    applicant_notification = queue_approval_progress_notification(
        background_tasks, db, record, "submitted", "manual"
    )
    return {
        "ok": True, "item": _record_dict(record, owner.name),
        "notification": notification,
        "applicant_notification": applicant_notification,
    }


@router.patch("/manual-waybills/{record_id}")
@router.patch("/admin/manual-waybills/{record_id}")
def update_manual_waybill(
    record_id: int, body: ManualWaybillBody, request: Request,
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None,
):
    background_tasks = background_tasks or BackgroundTasks()
    record = db.query(models.ManualWaybillRecord).filter(
        models.ManualWaybillRecord.id == record_id,
    ).first()
    if not record:
        raise HTTPException(404, "运单补充记录不存在")
    _assert_mutable(record, request)
    old_scope = SimpleNamespace(
        region_code=record.region_code, courier_type=record.courier_type,
        waybill_no=record.waybill_no,
    )
    user_id, role = _identity(request)
    owner_id = record.created_by
    if role == "admin" and body.created_by:
        owner_id = body.created_by
    owner = db.query(models.User).filter(models.User.id == owner_id).first()
    if not owner:
        raise HTTPException(400, "指定的员工不存在")
    auth_enabled = bool(getattr(request.state, "auth_enabled", False))
    automatic_sender = owner.name or ""
    values = _validated_values(db, body, sender_name=automatic_sender)
    duplicate = db.query(models.ManualWaybillRecord).filter(
        models.ManualWaybillRecord.id != record.id,
        models.ManualWaybillRecord.region_code == values["region_code"],
        models.ManualWaybillRecord.courier_type == values["courier_type"],
        models.ManualWaybillRecord.waybill_no == values["waybill_no"],
        models.ManualWaybillRecord.status.in_(("pending_approval", "approved")),
    ).first()
    if duplicate:
        raise HTTPException(409, "该区域和快递下已存在相同运单号的有效补充记录")
    for key, value in values.items():
        setattr(record, key, value)
    _reset_for_submission(record, db)
    if role == "admin" and body.created_by:
        record.created_by = owner.id
    db.commit()
    db.refresh(record)
    rematch_for_manual_record(db, old_scope)
    creator = db.query(models.User).filter(models.User.id == record.created_by).first()
    notification = queue_approval_notification(
        background_tasks, db, record, "manual"
    )
    applicant_notification = queue_approval_progress_notification(
        background_tasks, db, record, "submitted", "manual"
    )
    return {
        "ok": True, "item": _record_dict(record, creator.name if creator else ""),
        "notification": notification,
        "applicant_notification": applicant_notification,
    }


@router.post("/manual-waybills/{record_id}/withdraw")
@router.post("/admin/manual-waybills/{record_id}/withdraw")
def withdraw_manual_waybill(
    record_id: int, request: Request, db: Session = Depends(get_db),
):
    record = db.query(models.ManualWaybillRecord).filter(
        models.ManualWaybillRecord.id == record_id,
    ).first()
    if not record:
        raise HTTPException(404, "运单补充记录不存在")
    _assert_mutable(record, request)
    record.status = "withdrawn"
    db.commit()
    rematch_for_manual_record(db, record)
    return {"ok": True}


@router.post("/manual-waybills/{record_id}/approve")
def approve_manual_waybill(
    record_id: int, body: ManualWaybillDecision, request: Request,
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None,
):
    background_tasks = background_tasks or BackgroundTasks()
    record = db.query(models.ManualWaybillRecord).filter(
        models.ManualWaybillRecord.id == record_id,
    ).first()
    if not record:
        raise HTTPException(404, "运单补充记录不存在")
    if record.status != "pending_approval":
        raise HTTPException(400, "只有待二级审核的补充记录可以通过")
    _assert_level2_reviewer(record, request)
    record.status = "approved"
    record.approval_level2_time = models.china_now()
    record.approval_level2_remark = str(body.remark or "").strip()
    db.commit()
    rematch_for_manual_record(db, record)
    notification = queue_approval_result_notification(
        background_tasks, db, record, True,
        record.approval_level2_remark or "", "manual"
    )
    return {"ok": True, "approval_completed": True, "notification": notification}
@router.post("/manual-waybills/{record_id}/reject")
def reject_manual_waybill(
    record_id: int, body: ManualWaybillDecision, request: Request,
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None,
):
    background_tasks = background_tasks or BackgroundTasks()
    record = db.query(models.ManualWaybillRecord).filter(
        models.ManualWaybillRecord.id == record_id,
    ).first()
    if not record:
        raise HTTPException(404, "运单补充记录不存在")
    if record.status != "pending_approval":
        raise HTTPException(400, "只有待二级审核的补充记录可以驳回")
    _assert_level2_reviewer(record, request)
    record.status = "rejected"
    record.approval_level2_time = models.china_now()
    record.approval_level2_remark = str(body.remark or "").strip()
    db.commit()
    rematch_for_manual_record(db, record)
    notification = queue_approval_result_notification(
        background_tasks, db, record, False,
        record.approval_level2_remark or "", "manual"
    )
    return {"ok": True, "approval_completed": True, "notification": notification}
