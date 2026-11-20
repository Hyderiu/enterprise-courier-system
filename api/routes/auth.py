from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel

from config.settings import settings
from database.connection import get_db
from database import models
from utils.department_paths import build_department_path_map
from utils.regions import region_from_work_location, normalize_region

router = APIRouter()


class DingTalkLoginReq(BaseModel):
    auth_code: str
    next: str = "/"


def _safe_next(value: str) -> str:
    return value if value.startswith("/") and not value.startswith("//") else "/"


def _public_role(role: str | None) -> str:
    return "admin" if role in {"admin", "admin_approver"} else "employee"


def _user_payload(user: models.User) -> dict:
    return {"id": user.id, "name": user.name,
            "role": "admin" if user.role in {"admin", "admin_approver"} else "user",
            "department": user.department, "phone": user.phone, "avatar": user.avatar,
            "work_location": user.work_location or "",
            "region_code": normalize_region(user.region_code)}


def _hrm_field_text(field: dict, *, prefer_value: bool = False) -> str:
    keys = ("value", "label") if prefer_value else ("label", "value")
    for key in keys:
        value = field.get(key)
        if value not in (None, "", []):
            return str(value).strip()
    return ""


def _refresh_login_profile(client, user: models.User) -> str:
    """Refresh only the employee who is logging in; return their HRM manager userid."""
    try:
        rows = client.list_hrm_employee_fields([user.dingtalk_userid])
        row = next((item for item in rows if str(item.get("userid") or "") == str(user.dingtalk_userid)), None)
        fields = {
            str(item.get("field_code") or ""): item
            for item in ((row or {}).get("field_list") or [])
        }
        workplace = fields.get("sys00-workPlace") or {}
        value = _hrm_field_text(workplace)
        if value:
            user.work_location = value
            user.region_code = region_from_work_location(value)
        elif not user.region_code:
            user.region_code = region_from_work_location(user.work_location)

        # The employee-list response already contains all returned fields. Read
        # the manager and a named job-title field from this same single-person
        # call; do not call the paid field metadata API during login.
        manager_userid = _hrm_field_text(
            fields.get("sys00-reportManagerId") or {}, prefer_value=True,
        )
        for item in ((row or {}).get("field_list") or []):
            if str(item.get("field_name") or "").strip() in {"职务", "职位"}:
                job_title = _hrm_field_text(item)
                if job_title:
                    user.hrm_job_title = job_title
                break
        return manager_userid
    except Exception:
        if not user.region_code:
            user.region_code = region_from_work_location(user.work_location)
        return ""


def _initial_admin_available(db: Session) -> bool:
    marker = db.query(models.SystemConfig).filter(
        models.SystemConfig.config_key == "initial_admin_bootstrapped"
    ).first()
    real_admin = db.query(models.User.id).filter(
        models.User.role.in_(("admin", "admin_approver")),
        models.User.is_active.isnot(False),
        models.User.dingtalk_userid != "mock_001",
    ).first()
    return marker is None and real_admin is None


def _claim_initial_admin(db: Session, user: models.User) -> bool:
    """Promote the current verified DingTalk user once; ignore legacy mock data."""
    if not _initial_admin_available(db):
        return False
    mock_users = db.query(models.User).filter(
        models.User.dingtalk_userid == "mock_001"
    ).all()
    for mock_user in mock_users:
        db.delete(mock_user)
    user.role = "admin"
    db.add(models.SystemConfig(
        config_key="initial_admin_bootstrapped",
        config_value="1",
        description="真实钉钉账号已完成首位管理员初始化",
    ))
    return True


def _department_name_from_detail(db: Session, detail: dict):
    for dept_id in detail.get("dept_id_list", []) or []:
        department = db.query(models.Department).filter(
            models.Department.dingtalk_dept_id == str(dept_id)).first()
        if department:
            paths = build_department_path_map(db.query(models.Department).all())
            return paths.get(department.id, department.dept_name)
    return None


@router.post("/dingtalk")
def dingtalk_login(req: DingTalkLoginReq, request: Request, db: Session = Depends(get_db)):
    try:
        from dingtalk import DingTalkClient
        client = DingTalkClient()
        result = client.get_user_by_auth_code(req.auth_code)
        userid = result.get("userid")
        if not userid:
            raise HTTPException(400, "钉钉未返回用户标识")
        user = db.query(models.User).filter(models.User.dingtalk_userid == userid).first()
        first_login = user is None
        # Returning employees already have an authoritative local profile.  Login
        # used to make two additional DingTalk/HRM calls on every entry, which made
        # the workbench wait on three serial network round trips.  Only bootstrap a
        # profile on first login; later directory changes are handled by the explicit
        # administrator synchronization task and never block authentication.
        detail = client.get_user_detail(userid) if first_login else {}
        department_name = _department_name_from_detail(db, detail) if first_login else None
        if not user:
            user = models.User(
                dingtalk_userid=userid,
                dingtalk_unionid=result.get("unionid") or detail.get("unionid"),
                name=detail.get("name"), phone=detail.get("mobile"), avatar=detail.get("avatar"),
                department=department_name, role="user", is_active=True)
            db.add(user)
        else:
            user.dingtalk_unionid = user.dingtalk_unionid or result.get("unionid") or detail.get("unionid")
            user.name = detail.get("name") or user.name
            user.phone = detail.get("mobile") or user.phone
            user.avatar = detail.get("avatar") or user.avatar
            user.department = department_name or user.department
        detail_title = str(detail.get("title") or "").strip()
        if detail_title:
            user.hrm_job_title = detail_title
        db.flush()
        hrm_manager_userid = _refresh_login_profile(client, user) if first_login else ""
        # A manual approval setting is authoritative. Normal login must not silently
        # replace it with the directory manager; administrators can still request an
        # explicit Smart HR synchronization from the approval settings page.
        if first_login and user.approval_level1_required_override is None:
            manager_userid = hrm_manager_userid or str(detail.get("manager_userid") or "").strip()
            if manager_userid:
                manager = db.query(models.User).filter(
                    models.User.dingtalk_userid == manager_userid
                ).first()
                if manager and manager.id != user.id:
                    user.supervisor_user_id = manager.id
            else:
                # DingTalk explicitly returns no upward manager for top-level leaders.
                user.supervisor_user_id = None
        if user.is_active is False:
            raise HTTPException(403, "该员工账号已停用，请联系管理员")
        # 首次部署没有管理员时，由第一个通过企业钉钉验证的员工完成初始化。
        # 后续角色管理会阻止移除最后一名管理员，因此这条分支只会执行一次。
        _claim_initial_admin(db, user)
        db.commit()
        db.refresh(user)
        request.session.clear()
        request.session.update({"user_id": user.id, "dingtalk_userid": user.dingtalk_userid,
                                "name": user.name or "钉钉员工", "role": _public_role(user.role),
                                "department": user.department or "",
                                "region_code": normalize_region(user.region_code)})
        return {"ok": True, "first_login": first_login, "user": _user_payload(user),
                "redirect": _safe_next(req.next)}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(502, f"钉钉免登失败：{error}")


@router.get("/status")
def login_status(request: Request, db: Session = Depends(get_db)):
    if (
        not settings.dingtalk_login_enabled
        and settings.local_user_id
        and request.state.current_user
    ):
        return {"ok": True, "authenticated": True, "user": request.state.current_user}
    user_id = request.session.get("user_id")
    if not user_id:
        return {"ok": True, "authenticated": False}
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user or user.is_active is False:
        request.session.clear()
        return {"ok": True, "authenticated": False}
    return {"ok": True, "authenticated": True, "user": _user_payload(user)}


@router.get("/bootstrap-admin/status")
def bootstrap_admin_status(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(401, "请先通过钉钉登录")
    return {"ok": True, "available": _initial_admin_available(db)}


@router.post("/bootstrap-admin")
def bootstrap_current_admin(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    user = db.query(models.User).filter(models.User.id == user_id).first() if user_id else None
    if not user or user.is_active is False:
        raise HTTPException(401, "请先通过钉钉登录")
    if user.dingtalk_userid == "mock_001":
        raise HTTPException(403, "测试账号不能初始化管理员")
    if not _claim_initial_admin(db, user):
        raise HTTPException(409, "管理员已经完成初始化")
    db.commit()
    db.refresh(user)
    request.session["role"] = "admin"
    if request.state.current_user:
        request.state.current_user["role"] = "admin"
    return {"ok": True, "user": _user_payload(user), "message": "当前账号已设为管理员"}


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True, "redirect": "/dingtalk-entry"}


@router.get("/mock-user")
def mock_user(db: Session = Depends(get_db)):
    if settings.dingtalk_login_enabled or not settings.debug:
        raise HTTPException(404, "Not found")
    user = db.query(models.User).filter(
        models.User.dingtalk_userid != "mock_001", models.User.is_active.isnot(False)
    ).order_by(models.User.id).first()
    if not user:
        raise HTTPException(404, "暂无可用的本地测试成员")
    return {"ok": True, "user": _user_payload(user)}
