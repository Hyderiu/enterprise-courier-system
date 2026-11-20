from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import models
from database.connection import get_db


router = APIRouter()
ALLOWED_ROLES = {"user", "admin"}
ADMIN_ROLES = {"admin"}


class RoleUpdate(BaseModel):
    role: str


class ActiveUpdate(BaseModel):
    is_active: bool


class CustomSenderAddressUpdate(BaseModel):
    allowed: bool


def _serialize(user: models.User) -> dict:
    public_role = "admin" if (user.role or "user") in {"admin", "admin_approver"} else "user"
    return {
        "id": user.id,
        "dingtalk_userid": user.dingtalk_userid,
        "name": user.name or "未命名员工",
        "phone": user.phone or "",
        "avatar": user.avatar or "",
        "department": user.department or "未分配部门",
        "role": public_role,
        "allow_custom_sender_address": bool(user.allow_custom_sender_address),
        "is_active": user.is_active is not False,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def _assert_not_last_admin(db: Session, user: models.User, next_role=None, next_active=None):
    currently_admin = (user.role or "user") in ADMIN_ROLES and user.is_active is not False
    remains_admin = (
        (next_role if next_role is not None else user.role or "user") in ADMIN_ROLES
        and (next_active if next_active is not None else user.is_active is not False)
    )
    if currently_admin and not remains_admin:
        admin_count = db.query(models.User).filter(
            models.User.role.in_(ADMIN_ROLES), models.User.is_active.isnot(False)
        ).count()
        if admin_count <= 1:
            raise HTTPException(400, "不能移除或停用系统中最后一名管理员")


@router.get("/users")
def list_users(q: str = "", role: str = "", db: Session = Depends(get_db)):
    query = db.query(models.User)
    keyword = q.strip()
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(or_(
            models.User.name.like(like),
            models.User.phone.like(like),
            models.User.department.like(like),
            models.User.dingtalk_userid.like(like),
        ))
    if role == "admin":
        query = query.filter(models.User.role.in_(("admin", "admin_approver")))
    elif role == "user":
        query = query.filter(~models.User.role.in_(("admin", "admin_approver")))
    users = query.order_by(models.User.department, models.User.name).all()
    return {"ok": True, "total": len(users), "items": [_serialize(user) for user in users]}


@router.patch("/users/{user_id}/role")
def update_user_role(
    user_id: int, body: RoleUpdate, request: Request, db: Session = Depends(get_db)
):
    if body.role not in ALLOWED_ROLES:
        raise HTTPException(400, "无效的角色")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(404, "员工不存在")
    _assert_not_last_admin(db, user, next_role=body.role)
    user.role = body.role
    db.commit()
    db.refresh(user)
    if request.session.get("user_id") == user.id:
        public_role = "admin" if user.role == "admin" else "employee"
        request.session["role"] = public_role
        if request.state.current_user:
            request.state.current_user["role"] = public_role
    return {"ok": True, "user": _serialize(user)}


@router.patch("/users/{user_id}/active")
def update_user_active(user_id: int, body: ActiveUpdate, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(404, "员工不存在")
    _assert_not_last_admin(db, user, next_active=body.is_active)
    user.is_active = body.is_active
    db.commit()
    db.refresh(user)
    return {"ok": True, "user": _serialize(user)}


@router.patch("/users/{user_id}/custom-sender-address")
def update_custom_sender_address(
    user_id: int, body: CustomSenderAddressUpdate, request: Request,
    db: Session = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(404, "员工不存在")
    user.allow_custom_sender_address = body.allowed
    db.commit()
    db.refresh(user)
    if request.state.current_user and request.state.current_user.get("id") == user.id:
        request.state.current_user["allow_custom_sender_address"] = body.allowed
    return {"ok": True, "user": _serialize(user)}
