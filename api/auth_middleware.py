"""DingTalk browser-session gate and role authorization."""
from urllib.parse import quote

from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from config.settings import settings
from database.connection import SessionLocal
from database import models
from utils.regions import normalize_region

PUBLIC_EXACT = {
    "/dingtalk-entry", "/dingtalk-notification-entry",
    "/api/auth/dingtalk", "/api/auth/status", "/api/auth/logout",
    "/api/courier/status", "/api/health/crm-sync", "/api/health/reference-sync",
    "/api/health/monthly-exports",
    "/api/courier/sf/route-push", "/api/courier/sf-city/callback", "/api/dingtalk/event",
}
ADMIN_ROLES = {"admin", "admin_approver"}


def normalized_role(role):
    return "admin" if role in ADMIN_ROLES else "employee"


def is_public(path):
    return path in PUBLIC_EXACT or path.startswith("/static/")


def needs_approval_role(path):
    return path.startswith("/api/approval/") or (
        path.startswith("/api/batches/") and path.endswith(("/approve", "/reject", "/order")))


def needs_admin_role(path):
    feedback_path = path == "/admin/feedback" or path.startswith("/api/admin/feedback/")
    return (((path == "/admin" or path.startswith("/admin/"))
             and path not in {"/admin/approvals", "/admin/feedback"})
            or (path.startswith("/api/admin/") and not feedback_path)
            or path == "/api/orders/direct"
            or path == "/api/dingtalk/manual-trigger"
            or path == "/api/dingtalk/health"
            or path == "/api/dingtalk/oa-approval"
            or (path.startswith("/api/batches/") and path.endswith("/direct-order")))


class DingTalkSessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request.state.auth_enabled = settings.dingtalk_login_enabled
        request.state.current_user = None
        request.state.local_identity = False
        if not settings.dingtalk_login_enabled and settings.local_user_id:
            with SessionLocal() as db:
                local_user = db.query(models.User).filter(
                    models.User.id == int(settings.local_user_id)
                ).first()
                if local_user and local_user.is_active is not False:
                    request.state.current_user = {
                        "id": local_user.id, "name": local_user.name or "本地管理员",
                        "phone": local_user.phone or "",
                        "role": normalized_role(local_user.role),
                        "department": local_user.department or "",
                        "avatar": local_user.avatar or "",
                        "work_location": local_user.work_location or "",
                        "region_code": normalize_region(local_user.region_code),
                        "allow_custom_sender_address": bool(local_user.allow_custom_sender_address),
                    }
                    request.state.local_identity = True
        if not settings.dingtalk_login_enabled or is_public(request.url.path):
            return await call_next(request)
        user_id = request.session.get("user_id")
        user = None
        if user_id:
            with SessionLocal() as db:
                user = db.query(models.User).filter(models.User.id == user_id).first()
                if user and user.is_active is not False:
                    request.state.current_user = {
                        "id": user.id, "name": user.name or "钉钉员工",
                        "phone": user.phone or "",
                        "role": normalized_role(user.role), "department": user.department or "",
                        "avatar": user.avatar or "",
                        "work_location": user.work_location or "",
                        "region_code": normalize_region(user.region_code),
                        "allow_custom_sender_address": bool(user.allow_custom_sender_address)}
                else:
                    user = None
        if not user:
            request.session.clear()
            target = request.url.path + (("?" + request.url.query) if request.url.query else "")
            login_url = "/dingtalk-entry?next=" + quote(target, safe="")
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "请从钉钉工作台进入系统", "login_url": login_url}, status_code=401)
            return RedirectResponse(login_url, status_code=303)
        role = request.state.current_user["role"]
        if needs_admin_role(request.url.path) and role not in ADMIN_ROLES:
            return JSONResponse({"detail": "仅管理员可以访问"}, status_code=403)
        # 审批权由订单上当前指定审批人决定，不再单独维护“审核员”角色。
        # 所有登录员工可进入审批中心，真正提交时仍由
        # assert_assigned_approver 校验是否为当前审批人。
        return await call_next(request)
