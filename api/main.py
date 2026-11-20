from fastapi import FastAPI, Request, Depends
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi import HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import json
import os, sys
import re
from pathlib import Path
from urllib.parse import quote
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import SessionLocal, get_db
from database import models
from utils.department_paths import build_department_path_map
from utils.order_export import build_order_export
from api.routes import orders, approval, courier as courier_route, sf_city_callback, sf_city_orders, auth, dingtalk_webhook, cost as cost_route, batch as batch_route, admin_users, admin_addresses, billing_allocation, manual_waybills
from api.auth_middleware import DingTalkSessionMiddleware, normalized_role
from config.settings import settings
from utils.session_secret import get_session_secret
from utils.dingtalk_notification_auth import verify_notification_login_token
from utils.signed_session import SignedCookieSessionMiddleware
from utils.approval_policy import (
    LEVEL1_USER_KEY, LEVEL2_USER_KEY, LEVEL_COUNT_KEY, LEVEL1_ENABLED_KEY,
    REMINDER_INTERVAL_MINUTES_KEY,
    get_approval_policy, refresh_pending_approval, requires_level1_approval, save_config,
)
from utils.approval_notifications import queue_feedback_notification
from utils.sender_addresses import ensure_company_sender_addresses, user_can_custom_sender_address
from utils.order_status import (
    ORDER_STATUS_LABELS, classify_order_status, classify_track_event,
    order_status_display, preferred_track_status,
)
from utils.monthly_exports import list_monthly_exports, monthly_export_path

@asynccontextmanager
async def app_lifespan(_app):
    # 正式环境直接以 uvicorn api.main:app 启动，调度器必须跟随应用生命周期。
    from scheduler.scheduler import start_scheduler, shutdown_scheduler
    from utils.regions import ensure_default_service_catalog

    with SessionLocal() as db:
        if ensure_default_service_catalog(db):
            db.commit()
    if settings.scheduler_enabled:
        start_scheduler()
    try:
        yield
    finally:
        if settings.scheduler_enabled:
            shutdown_scheduler()


app = FastAPI(
    title="Express System",
    version="1.1.0",
    lifespan=app_lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    openapi_url="/openapi.json" if settings.debug else None,
)
app.add_middleware(DingTalkSessionMiddleware)
app.add_middleware(
    SignedCookieSessionMiddleware,
    secret_key=get_session_secret(settings.session_secret),
    session_cookie="courier_session",
    max_age=settings.session_max_age,
    same_site="lax",
    https_only=settings.session_cookie_secure,
)

static_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

APP_VERSION = (
    Path(__file__).resolve().parents[1].joinpath("VERSION").read_text(encoding="utf-8").strip()
)


@app.middleware("http")
async def disable_html_page_cache(request: Request, call_next):
    """Keep DingTalk's embedded browser from reusing an old inline-script page."""
    response = await call_next(request)
    response.headers["X-Courier-Version"] = APP_VERSION
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    content_type = str(response.headers.get("content-type") or "").lower()
    if content_type.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/api/version")
def application_version():
    return {"ok": True, "version": APP_VERSION}


@app.get("/api/health/crm-sync")
def crm_sync_health():
    """公开安全状态仅暴露 CRM 同步阶段和计数，不返回凭证。"""
    state = cost_route._crm_sync_snapshot()
    stats = state.get("stats") if isinstance(state.get("stats"), dict) else {}
    status = state.get("status") or "idle"
    return {
        "ok": True,
        "status": status,
        "message": (
            state.get("message") if status in {"running", "done"}
            else "同步失败" if status == "failed"
            else "等待首次自动同步"
        ),
        "phase": stats.get("phase"),
        "department_count": stats.get("department_count", 0),
        "project_count": stats.get("project_count", 0),
        "worker_alive": bool(state.get("worker_alive")),
        "error_type": stats.get("error_type") if status == "failed" else None,
        "updated_at": state.get("updated_at"),
        "started_at": state.get("started_at"),
    }


@app.get("/api/health/reference-sync")
def reference_sync_health():
    """Safe aggregate status for the daily members/departments/projects refresh."""
    crm = cost_route._crm_sync_snapshot()
    people = cost_route._department_sync_snapshot()
    supervisor = cost_route._supervisor_sync_snapshot()
    crm_stats = crm.get("stats") if isinstance(crm.get("stats"), dict) else {}
    people_stats = people.get("stats") if isinstance(people.get("stats"), dict) else {}
    member_stats = (
        people_stats.get("users_synced")
        if isinstance(people_stats.get("users_synced"), dict) else {}
    )
    automatic_statuses = [
        crm.get("status") or "idle",
        people.get("status") or "idle",
    ]
    overall = (
        "failed" if "failed" in automatic_statuses
        else "running" if "running" in automatic_statuses
        else "done" if automatic_statuses == ["done", "done"]
        else "idle"
    )
    return {
        "ok": overall != "failed",
        "status": overall,
        "schedule": "员工每周一 09:30；CRM 每天 09:30",
        "employee_schedule": "每周一 09:30",
        "crm_schedule": "每天 09:30",
        "schedule_weekday": "monday",
        "schedule_hour": 9,
        "schedule_minute": 30,
        "departments": {
            "status": automatic_statuses[0],
            "message": crm.get("message") or "等待首次自动同步",
            "total": crm_stats.get("department_count", 0),
        },
        "members": {
            "status": automatic_statuses[1],
            "total": member_stats.get("total", 0),
            "created": member_stats.get("created", 0),
            "updated": member_stats.get("updated", 0),
            "unchanged": member_stats.get("unchanged", 0),
        },
        "supervisors": {
            "status": supervisor.get("status") or "idle",
            "mode": "员工登录时更新本人；管理员可手动全量同步",
            "message": supervisor.get("message") or "未执行手动全量同步",
            "total": supervisor.get("total", 0),
            "read": supervisor.get("read", 0),
            "failed": supervisor.get("failed", 0),
        },
        "projects": {
            "status": automatic_statuses[0],
            "message": crm.get("message") or "等待首次自动同步",
            "total": crm_stats.get("project_count", crm_stats.get("current_count", 0)),
        },
    }


@app.get("/api/health/monthly-exports")
def monthly_export_health():
    """Safe status for monthly files; never expose recipients or filesystem paths."""
    exports = list_monthly_exports()
    if not exports:
        return {
            "ok": True, "status": "idle", "latest_month": None,
            "schedule": "缺失文件在服务启动时静默补生成；每月1日10:00发送提醒",
            "files_ready": False, "notification_sent": False,
        }
    latest = exports[0]
    notification = latest.get("notification") or {}
    notification_suppressed = bool(notification.get("suppressed"))
    return {
        "ok": True,
        "status": "done" if notification.get("sent") else "files_ready",
        "latest_month": latest.get("month"),
        "schedule": "缺失文件在服务启动时静默补生成；每月1日10:00发送提醒",
        "files_ready": True,
        "sf_count": ((latest.get("files") or {}).get("all-SF") or
                     (latest.get("files") or {}).get("SF") or {}).get("count", 0),
        "db_count": ((latest.get("files") or {}).get("all-DB") or
                     (latest.get("files") or {}).get("DB") or {}).get("count", 0),
        "jdl_count": ((latest.get("files") or {}).get("all-JDL") or
                      (latest.get("files") or {}).get("JDL") or {}).get("count", 0),
        "notification_sent": bool(notification.get("sent")),
        "notification_suppressed": notification_suppressed,
        "recipient_count": notification.get("recipient_count", 0),
    }

templates_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "templates")
templates = Jinja2Templates(directory=templates_dir)
templates.env.globals["app_version"] = APP_VERSION

DEFAULT_ITEM_TYPES = [
    "办公用品", "文件", "日用品", "电子产品", "服装", "其他",
    "项目道具", "抽奖礼品", "节日礼品",
]

def _order_category(order):
    return classify_order_status(order.status, order.track_status, bool(order.courier_order_no))


def _latest_track_text_by_order(db, orders):
    order_ids = [order.id for order in orders]
    if not order_ids:
        return {}
    rows = db.query(models.CourierTrack).filter(
        models.CourierTrack.order_id.in_(order_ids)
    ).order_by(
        models.CourierTrack.order_id, models.CourierTrack.track_time.desc(),
        models.CourierTrack.id.desc(),
    ).all()
    result = {}
    for row in rows:
        result.setdefault(row.order_id, {
            "status": row.track_status or "",
            "detail": row.track_detail or "",
            "text": f"{row.track_status or ''} {row.track_detail or ''}",
        })
    return result


def _category_with_latest_track(order, latest_track_texts):
    latest = latest_track_texts.get(order.id) or {}
    if order.status == "cancelled":
        return "cancelled"
    if order.status == "rejected":
        return "rejected"
    if order.status == "failed":
        return "exception"
    if order.status == "delivered":
        return "delivered"
    track_text = latest.get("text") or order.track_status
    return classify_order_status(
        order.status, track_text, bool(order.courier_order_no),
        baseline_track_text=order.track_status or "",
    )


def _apply_order_display_statuses(db, order_rows, latest_tracks=None):
    """Attach the same canonical status used by lists, drawers, filters and exports."""
    latest_tracks = latest_tracks if latest_tracks is not None else _latest_track_text_by_order(db, order_rows)
    for order in order_rows:
        latest = latest_tracks.get(order.id) or {}
        track_text = latest.get("text") or order.track_status or ""
        display = order_status_display(
            order.status, track_text, bool(order.courier_order_no),
            baseline_track_text=order.track_status or "",
        )
        order.display_status_category = display["status_category"]
        order.display_status_label = display["status_label"]
        order.display_track_status = preferred_track_status(
            order.track_status or "", latest.get("status") or "", latest.get("detail") or ""
        )
        order.can_resubmit = orders.order_can_resubmit(order)
    return latest_tracks


def _get_item_types(db: Session) -> list[str]:
    row = db.query(models.SystemConfig).filter(
        models.SystemConfig.config_key == "item_types"
    ).first()
    if row and row.config_value:
        try:
            values = json.loads(row.config_value)
            if isinstance(values, list):
                cleaned = [str(value).strip() for value in values if str(value).strip()]
                if cleaned:
                    return cleaned[:15]
        except (TypeError, ValueError):
            pass
    return DEFAULT_ITEM_TYPES.copy()


def _admin_order_filters(request: Request):
    from utils.regions import REGION_CODES
    today = datetime.now().date()

    def parse_date(value, fallback):
        try:
            return datetime.strptime(value or "", "%Y-%m-%d").date()
        except ValueError:
            return fallback

    requested_month = (request.query_params.get("month") or "").strip()
    requested_start_month = (request.query_params.get("start_month") or "").strip()
    requested_end_month = (request.query_params.get("end_month") or "").strip()
    legacy_start = request.query_params.get("start_date")
    legacy_end = request.query_params.get("end_date")
    if requested_month:
        try:
            month_start = datetime.strptime(requested_month, "%Y-%m").date().replace(day=1)
        except ValueError:
            month_start = today.replace(day=1)
        next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        start_date = month_start
        end_date = next_month - timedelta(days=1)
        start_month = end_month = month_start.strftime("%Y-%m")
    elif requested_start_month or requested_end_month or not (legacy_start or legacy_end):
        current_month = today.replace(day=1)
        previous_month = (current_month - timedelta(days=1)).replace(day=1)
        try:
            start_date = datetime.strptime(requested_start_month, "%Y-%m").date().replace(day=1)
        except ValueError:
            start_date = previous_month
        try:
            end_month_date = datetime.strptime(requested_end_month, "%Y-%m").date().replace(day=1)
        except ValueError:
            end_month_date = current_month
        if start_date > end_month_date:
            start_date, end_month_date = end_month_date, start_date
        next_month = (end_month_date.replace(day=28) + timedelta(days=4)).replace(day=1)
        end_date = next_month - timedelta(days=1)
        start_month = start_date.strftime("%Y-%m")
        end_month = end_month_date.strftime("%Y-%m")
    else:
        # Keep old bookmarked/export links valid while the UI now uses a month.
        start_date = parse_date(legacy_start, today.replace(day=1))
        end_date = parse_date(legacy_end, today)
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        start_month = start_date.strftime("%Y-%m")
        end_month = end_date.strftime("%Y-%m")
    requested_region = (request.query_params.get("region") or "all").strip()
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "month": start_month if start_month == end_month else "",
        "start_month": start_month,
        "end_month": end_month,
        "waybill": (request.query_params.get("waybill") or "").strip(),
        "sender_name": (request.query_params.get("sender_name") or "").strip(),
        "sender_phone": (request.query_params.get("sender_phone") or "").strip(),
        "receiver_name": (request.query_params.get("receiver_name") or "").strip(),
        "receiver_phone": (request.query_params.get("receiver_phone") or "").strip(),
        "employee": (request.query_params.get("employee") or "").strip(),
        "status": (request.query_params.get("status") or "").strip(),
        "include_cancelled": request.query_params.get("include_cancelled", "1") == "1",
        "region": requested_region if requested_region in REGION_CODES else "all",
    }


def _visible_orders_query(db: Session):
    """Exclude batch quote/precheck drafts until the applicant submits approval."""
    draft_batch_ids = db.query(models.Batch.id).filter(
        models.Batch.status == "draft"
    )
    return db.query(models.Order).filter(or_(
        models.Order.batch_id.is_(None),
        ~models.Order.batch_id.in_(draft_batch_ids),
    ))


def _filtered_admin_orders(db: Session, filters, courier_type=None):
    start_date = datetime.strptime(filters["start_date"], "%Y-%m-%d").date()
    end_date = datetime.strptime(filters["end_date"], "%Y-%m-%d").date()
    unfinalized_batch_ids = db.query(models.Batch.id).filter(
        models.Batch.status.notin_(("done", "partial", "failed"))
    )
    query = _visible_orders_query(db).filter(or_(
        models.Order.batch_id.is_(None),
        ~models.Order.batch_id.in_(unfinalized_batch_ids),
    )).outerjoin(
        models.User, models.Order.created_by == models.User.id
    ).filter(
        models.Order.created_at >= datetime.combine(start_date, datetime.min.time()),
        models.Order.created_at <= datetime.combine(end_date, datetime.max.time()),
    )
    if courier_type:
        query = query.filter(models.Order.courier_type == courier_type)
    if filters.get("region") and filters["region"] != "all":
        query = query.filter(models.Order.region_code == filters["region"])
    if filters["waybill"]:
        like = f"%{filters['waybill']}%"
        query = query.filter(
            models.Order.courier_order_no.like(like) | models.Order.order_no.like(like)
        )
    for column, key in (
        (models.Order.sender_name, "sender_name"),
        (models.Order.sender_phone, "sender_phone"),
        (models.Order.receiver_name, "receiver_name"),
        (models.Order.receiver_phone, "receiver_phone"),
    ):
        if filters[key]:
            query = query.filter(column.like(f"%{filters[key]}%"))
    if filters["employee"]:
        query = query.filter(models.User.name.like(f"%{filters['employee']}%"))
    if not filters["include_cancelled"]:
        query = query.filter(models.Order.status != "cancelled")
    result = query.order_by(models.Order.created_at.desc()).all()
    latest_tracks = _apply_order_display_statuses(db, result)
    if filters["status"] in ORDER_STATUS_LABELS:
        result = [
            order for order in result
            if _category_with_latest_track(order, latest_tracks) == filters["status"]
        ]
    return result


def _filtered_admin_batches(db: Session, filters, courier_type=None):
    """Submitted batches stay as one row until formal per-ticket ordering finishes."""
    start_date = datetime.strptime(filters["start_date"], "%Y-%m-%d").date()
    end_date = datetime.strptime(filters["end_date"], "%Y-%m-%d").date()
    query = db.query(models.Batch).outerjoin(
        models.User, models.Batch.created_by == models.User.id
    ).filter(
        models.Batch.status.in_(("pending_approval", "approved", "ordering", "rejected")),
        models.Batch.created_at >= datetime.combine(start_date, datetime.min.time()),
        models.Batch.created_at <= datetime.combine(end_date, datetime.max.time()),
    )
    if courier_type:
        query = query.filter(models.Batch.courier_type == courier_type)
    if filters.get("region") and filters["region"] != "all":
        query = query.filter(models.Batch.region_code == filters["region"])
    if filters["waybill"]:
        query = query.filter(models.Batch.batch_no.like(f"%{filters['waybill']}%"))
    if filters["sender_name"]:
        query = query.filter(models.Batch.sender_name.like(f"%{filters['sender_name']}%"))
    if filters["sender_phone"]:
        query = query.filter(models.Batch.sender_phone.like(f"%{filters['sender_phone']}%"))
    if filters["employee"]:
        query = query.filter(models.User.name.like(f"%{filters['employee']}%"))
    if filters["receiver_name"] or filters["receiver_phone"]:
        child_query = db.query(models.Order.batch_id).filter(models.Order.batch_id.isnot(None))
        if filters["receiver_name"]:
            child_query = child_query.filter(
                models.Order.receiver_name.like(f"%{filters['receiver_name']}%")
            )
        if filters["receiver_phone"]:
            child_query = child_query.filter(
                models.Order.receiver_phone.like(f"%{filters['receiver_phone']}%")
            )
        query = query.filter(models.Batch.id.in_(child_query))
    if filters["status"]:
        if filters["status"] == "pending":
            query = query.filter(models.Batch.status == "pending_approval")
        elif filters["status"] == "rejected":
            query = query.filter(models.Batch.status == "rejected")
        else:
            return []
    return query.order_by(models.Batch.created_at.desc()).all()

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(orders.router, prefix="/api/orders", tags=["orders"])
app.include_router(approval.router, prefix="/api/approval", tags=["approval"])
app.include_router(courier_route.router, prefix="/api/courier", tags=["courier"])
app.include_router(sf_city_callback.router, prefix="/api/courier", tags=["sf-city-callback"])
# Always register the route. Runtime access is controlled by the Xiamen
# production setting so enabling it in the database does not require a code
# restart merely to make the router exist.
app.include_router(sf_city_orders.router, prefix="/api/sf-city", tags=["sf-city-orders"])
app.include_router(dingtalk_webhook.router, prefix="/api/dingtalk", tags=["dingtalk"])
app.include_router(cost_route.router, prefix="/api", tags=["cost"])
app.include_router(batch_route.router, prefix="/api/batches", tags=["batch"])
app.include_router(admin_users.router, prefix="/api/admin", tags=["admin-users"])
app.include_router(admin_addresses.router, prefix="/api/admin", tags=["admin-addresses"])
app.include_router(billing_allocation.router, prefix="/api/admin", tags=["billing-allocation"])
app.include_router(manual_waybills.router, prefix="/api", tags=["manual-waybills"])


@app.get("/dingtalk-entry", response_class=HTMLResponse)
async def dingtalk_entry(request: Request):
    next_path = request.query_params.get("next") or "/"
    if not next_path.startswith("/") or next_path.startswith("//"):
        next_path = "/"
    corp_id = request.query_params.get("corpid") or settings.dingtalk_corp_id
    if corp_id == "$CORPID$":
        corp_id = ""
    return templates.TemplateResponse(request, name="dingtalk_entry.html", context={
        "client_id": settings.dingtalk_app_key,
        "corp_id": corp_id,
        "next": next_path,
    })


@app.get("/dingtalk-notification-entry")
async def dingtalk_notification_entry(
    request: Request, token: str, db: Session = Depends(get_db)
):
    try:
        payload = verify_notification_login_token(token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    user = db.query(models.User).filter(
        models.User.id == int(payload["uid"]),
        models.User.dingtalk_userid == str(payload["did"]),
        models.User.is_active.is_(True),
    ).first()
    if not user:
        raise HTTPException(status_code=401, detail="工作提醒对应的员工账号不存在或已停用")
    request.session.clear()
    request.session.update({
        "user_id": user.id,
        "dingtalk_userid": user.dingtalk_userid,
        "name": user.name or "钉钉员工",
        "role": normalized_role(user.role),
        "department": user.department or "",
        "region_code": user.region_code or "xiamen_other",
    })
    return RedirectResponse(str(payload["target"]), status_code=303)


@app.get("/express", response_class=HTMLResponse)
async def express_page(request: Request, db: Session = Depends(get_db)):
    from utils.regions import REGION_LABELS, region_options, request_region
    total = db.query(models.Order).count()
    pending = db.query(models.Order).filter(models.Order.status == "pending").count()
    shipped = db.query(models.Order).filter(models.Order.status.in_(["ordered", "success", "delivered"])).count()
    ensure_company_sender_addresses(db)
    current_user = request.state.current_user
    debug_region_switch = not bool(getattr(request.state, "auth_enabled", False))
    region_code = request_region(
        db, current_user, request.query_params.get("region"),
        auth_enabled=not debug_region_switch,
    )
    sender_addrs = db.query(models.Address).filter(
        models.Address.scope == "company", models.Address.role == "sender",
        models.Address.region_code == region_code,
    ).order_by(models.Address.id).all()
    sender_address_items = [{
        "id": address.id, "name": address.label or "企业寄件地址",
        "company": address.company_name or "", "province": address.province or "",
        "city": address.city or "", "district": address.district or "",
        "detail": address.detail or "",
    } for address in sender_addrs]
    resubmit_order = None
    raw_resubmit_id = str(request.query_params.get("resubmit_order_id") or "").strip()
    if raw_resubmit_id:
        if not raw_resubmit_id.isdigit():
            raise HTTPException(status_code=400, detail="重新提交的订单编号无效")
        source_order = db.query(models.Order).filter(
            models.Order.id == int(raw_resubmit_id)
        ).first()
        if not source_order:
            raise HTTPException(status_code=404, detail="需要编辑的原订单不存在")
        if (
            bool(getattr(request.state, "auth_enabled", False))
            and str((current_user or {}).get("role") or "employee") != "admin"
            and int((current_user or {}).get("id") or 0) != int(source_order.created_by or 0)
        ):
            raise HTTPException(status_code=403, detail="只能编辑并重新提交自己的订单")
        if not orders.order_can_resubmit(source_order):
            raise HTTPException(
                status_code=400,
                detail="只有审核失败或尚未生成运单号的下单失败记录可以编辑并重新提交",
            )
        resubmit_order = orders._to_dict(source_order)
    from utils.sf_city_access import sf_city_region_enabled, sf_city_user_allowed
    sf_city_available = (
        sf_city_region_enabled(db, region_code)
        and sf_city_user_allowed(
            db, current_user or {},
            auth_enabled=bool(getattr(request.state, "auth_enabled", False)),
            region_code=region_code,
        )
    )
    return templates.TemplateResponse(
        request, name="index.html",
        context={"stats": {"total": total, "pending": pending, "shipped": shipped},
                 "sender_addresses": sender_addrs,
                 "sender_address_items": sender_address_items,
                 "item_types": _get_item_types(db),
                 "region_code": region_code,
                 "region_label": REGION_LABELS[region_code],
                 "regions": region_options(),
                 "debug_region_switch": debug_region_switch,
                 "current_user_department": (current_user or {}).get("department", ""),
                 "allow_custom_sender_address": user_can_custom_sender_address(db, current_user),
                 "sf_city_available": sf_city_available,
                 "resubmit_order": resubmit_order}
    )


@app.get("/sf-city", response_class=HTMLResponse)
async def sf_city_order_page(request: Request, db: Session = Depends(get_db)):
    current_user = request.state.current_user or {}
    from utils.regions import BEIJING, REGION_LABELS, request_region
    from utils.sf_city_access import require_sf_city_user
    region_code = request_region(
        db, current_user, request.query_params.get("region"),
        auth_enabled=bool(getattr(request.state, "auth_enabled", False)),
    )
    require_sf_city_user(
        db, current_user, region_code=region_code,
        auth_enabled=bool(getattr(request.state, "auth_enabled", False)),
    )
    is_beijing = region_code == BEIJING
    return templates.TemplateResponse(request, name="sf_city_order.html", context={
        "current_user_department": current_user.get("department") or "",
        "region_code": region_code,
        "region_label": REGION_LABELS[region_code],
        "receiver_province": "北京市" if is_beijing else "福建省",
        "receiver_city": "北京市" if is_beijing else "厦门市",
        "receiver_districts": (
            ["东城区", "西城区", "朝阳区", "海淀区", "丰台区", "石景山区", "通州区", "顺义区", "昌平区", "大兴区", "房山区", "门头沟区", "怀柔区", "平谷区", "密云区", "延庆区"]
            if is_beijing else ["思明区", "湖里区", "集美区", "海沧区", "同安区", "翔安区"]
        ),
        "pickup_notice": "北京公司登记取件地址" if is_beijing else "三楼指定取件点",
        "sf_city_product_types": [
            {"value": code, "label": label}
            for code, label in sf_city_orders.REGION_PRODUCT_TYPES[region_code].items()
        ],
    })

@app.get("/", response_class=HTMLResponse)
async def landing(request: Request, db: Session = Depends(get_db)):
    # 首页按日期范围展示寄件运单状态；默认最近 7 天（含今天）。
    today = datetime.now().date()
    default_start = today - timedelta(days=6)

    def parse_date(value, fallback):
        try:
            return datetime.strptime(value or "", "%Y-%m-%d").date()
        except ValueError:
            return fallback

    start_date = parse_date(request.query_params.get("start_date"), default_start)
    end_date = parse_date(request.query_params.get("end_date"), today)
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    range_start = datetime.combine(start_date, datetime.min.time())
    range_end = datetime.combine(end_date, datetime.max.time())

    current_user = request.state.current_user
    user_id_int = current_user["id"] if current_user else None

    query = _visible_orders_query(db).filter(
        models.Order.created_at >= range_start,
        models.Order.created_at <= range_end,
    )
    if user_id_int:
        query = query.filter(models.Order.created_by == user_id_int)
    elif settings.preview_safe_mode:
        # A no-login local preview has no employee identity. Never turn that
        # absence into access to every employee's personal order history.
        query = query.filter(models.Order.id < 0)
    orders_in_range = query.order_by(models.Order.created_at.desc()).all()
    latest_tracks = _latest_track_text_by_order(db, orders_in_range)

    counts = {
        "total": len(orders_in_range), "pending": 0, "pending_pickup": 0,
        "picked": 0, "transit": 0, "delivering": 0, "delivered": 0,
        "rejected": 0, "exception": 0,
    }
    for order in orders_in_range:
        category = _category_with_latest_track(order, latest_tracks)
        if category in counts:
            counts[category] += 1

    from utils.regions import user_region
    from utils.sf_city_access import sf_city_region_enabled, sf_city_user_allowed
    current_region = user_region(db, current_user)
    sf_city_available = sf_city_region_enabled(db, current_region) and sf_city_user_allowed(
        db, current_user or {},
        auth_enabled=bool(getattr(request.state, "auth_enabled", False)),
        region_code=current_region,
    )
    return templates.TemplateResponse(request, name="landing.html", context={
        "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
        "counts": counts,
        "user_id": user_id_int,
        "sf_city_available": sf_city_available,
    })


@app.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request, db: Session = Depends(get_db)):
    current_user = request.state.current_user
    user_id = current_user["id"] if current_user else None
    q = _visible_orders_query(db).order_by(models.Order.created_at.desc())
    # 员工订单页按包裹逐票展示，批量寄件产生的子订单也属于员工自己的订单。
    if user_id:
        q = q.filter(models.Order.created_by == int(user_id))
    elif settings.preview_safe_mode:
        q = q.filter(models.Order.id < 0)
    items = q.limit(100).all()
    _apply_order_display_statuses(db, items)
    creator_map = {}
    if user_id and current_user:
        creator_map[int(user_id)] = current_user.get("name") or "未命名员工"
    return templates.TemplateResponse(request, name="orders.html", context={
        "orders": items, "batches": [], "current_user_id": user_id,
        "admin_mode": False, "creator_map": creator_map,
    })


@app.get("/orders/{order_id}/track", response_class=HTMLResponse)
async def order_track_page(order_id: int, request: Request, db: Session = Depends(get_db)):
    order = db.query(models.Order).filter(models.Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    current_user = request.state.current_user
    role = str((current_user or {}).get("role") or "employee")
    if role == "employee" and current_user and order.created_by != int(current_user["id"]):
        raise HTTPException(status_code=403, detail="无权查看该订单物流")
    _apply_order_display_statuses(db, [order])
    return templates.TemplateResponse(request, name="order_track.html", context={"order": order})


@app.get("/admin/orders", response_class=HTMLResponse)
async def admin_orders_page(request: Request, db: Session = Depends(get_db)):
    from utils.regions import region_options
    filters = _admin_order_filters(request)
    candidate_orders = _filtered_admin_orders(db, filters)
    candidate_batches = _filtered_admin_batches(db, filters)
    user_ids = {order.created_by for order in candidate_orders if order.created_by}
    user_ids.update(batch.created_by for batch in candidate_batches if batch.created_by)
    users = db.query(models.User).filter(models.User.id.in_(user_ids)).all() if user_ids else []
    creator_map = {user.id: user.name or "未命名员工" for user in users}
    return templates.TemplateResponse(request, name="orders.html", context={
        "orders": candidate_orders, "batches": candidate_batches, "current_user_id": None,
        "admin_mode": True, "filters": filters, "creator_map": creator_map,
        "status_labels": ORDER_STATUS_LABELS,
        "result_count": len(candidate_orders) + len(candidate_batches),
        "approval_preview_mode": not bool(getattr(request.state, "auth_enabled", False)),
        "regions": region_options(),
    })


@app.get("/api/admin/orders/export")
async def export_admin_orders(request: Request, db: Session = Depends(get_db)):
    courier_type = (request.query_params.get("courier_type") or "").strip().upper()
    if courier_type not in {"SF", "DB", "JDL"}:
        raise HTTPException(status_code=400, detail="不支持该快递公司")
    filters = _admin_order_filters(request)
    export_orders = _filtered_admin_orders(db, filters, courier_type=courier_type)
    user_ids = {order.created_by for order in export_orders if order.created_by}
    users = db.query(models.User).filter(models.User.id.in_(user_ids)).all() if user_ids else []
    creator_details = {
        user.id: {"name": user.name or "未命名员工", "department": user.department or ""}
        for user in users
    }
    from utils.regions import REGION_LABELS
    region_name = (
        "全部区域" if filters["region"] == "all"
        else REGION_LABELS.get(filters["region"], filters["region"])
    )
    carrier_name = {"SF": "顺丰", "DB": "德邦", "JDL": "京东"}[courier_type]
    stream = build_order_export(
        export_orders, creator_details, _order_category, ORDER_STATUS_LABELS,
        region_name=region_name, carrier_name=carrier_name,
    )
    filename = f"{carrier_name}寄件详情-{datetime.now():%Y%m%d}.xlsx"
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.get("/addresses", response_class=HTMLResponse)
async def addresses_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, name="addresses.html", context={})


@app.get("/admin/monthly-exports", response_class=HTMLResponse)
async def admin_monthly_exports(request: Request):
    from utils.billing_allocation import CARRIERS, REGION_NAMES
    from utils.monthly_exports import previous_month
    selected_month = (request.query_params.get("month") or previous_month()).strip()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", selected_month):
        selected_month = previous_month()
    selected_region = (request.query_params.get("region") or "xiamen_other").strip()
    if selected_region not in {*REGION_NAMES, "all"}:
        selected_region = "xiamen_other"
    selected_courier = (request.query_params.get("courier_type") or "SF").strip().upper()
    if selected_courier not in {*CARRIERS, "ALL"}:
        selected_courier = "SF"
    return templates.TemplateResponse(request, name="admin_monthly_exports.html", context={
        "selected_month": selected_month, "selected_region": selected_region,
        "selected_courier": selected_courier,
        "region_names": REGION_NAMES, "carriers": CARRIERS,
    })


@app.get("/admin/billing-visualization", response_class=HTMLResponse)
async def admin_billing_visualization(request: Request):
    from utils.billing_allocation import CARRIERS, REGION_NAMES
    from utils.monthly_exports import previous_month
    fallback_month = (request.query_params.get("month") or previous_month()).strip()
    selected_start_month = (
        request.query_params.get("start_month") or fallback_month
    ).strip()
    selected_end_month = (
        request.query_params.get("end_month") or fallback_month
    ).strip()
    month_pattern = r"\d{4}-(0[1-9]|1[0-2])"
    if not re.fullmatch(month_pattern, selected_start_month):
        selected_start_month = previous_month()
    if not re.fullmatch(month_pattern, selected_end_month):
        selected_end_month = selected_start_month
    if selected_start_month > selected_end_month:
        selected_start_month, selected_end_month = selected_end_month, selected_start_month
    selected_region = (request.query_params.get("region") or "all").strip()
    if selected_region not in {*REGION_NAMES, "all"}:
        selected_region = "all"
    selected_courier = (request.query_params.get("courier_type") or "all").strip().upper()
    if selected_courier not in {*CARRIERS, "ALL"}:
        selected_courier = "ALL"
    return templates.TemplateResponse(
        request, name="billing_visualization.html", context={
            "selected_start_month": selected_start_month,
            "selected_end_month": selected_end_month,
            "selected_region": selected_region,
            "selected_courier": selected_courier,
            "region_names": REGION_NAMES, "carriers": CARRIERS,
        },
    )


@app.get("/waybill-records", response_class=HTMLResponse)
async def employee_waybill_records(request: Request):
    current = getattr(request.state, "current_user", None) or {}
    return templates.TemplateResponse(request, name="waybill_records.html", context={
        "admin_mode": False, "debug_mode": not bool(request.state.auth_enabled),
        "default_region": current.get("region_code") or "xiamen_other",
    })


@app.get("/admin/waybill-records", response_class=HTMLResponse)
async def admin_waybill_records(request: Request):
    current = getattr(request.state, "current_user", None) or {}
    return templates.TemplateResponse(request, name="waybill_records.html", context={
        "admin_mode": True, "debug_mode": not bool(request.state.auth_enabled),
        "default_region": current.get("region_code") or "xiamen_other",
    })


@app.get("/api/admin/monthly-exports/{month}/{courier_type}")
async def download_monthly_export(month: str, courier_type: str, region: str = "all"):
    path, filename = monthly_export_path(month, courier_type, region_code=region)
    if not path:
        raise HTTPException(status_code=404, detail="月结文件不存在")
    return FileResponse(
        path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/admin/addresses", response_class=HTMLResponse)
async def admin_addresses_page(request: Request, db: Session = Depends(get_db)):
    from utils.regions import normalize_region, region_options
    ensure_company_sender_addresses(db)
    return templates.TemplateResponse(request, name="admin_addresses.html", context={
        "regions": region_options(),
        "selected_region": normalize_region(request.query_params.get("region")),
    })


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    now = datetime.now()
    today = now.date()
    ts = datetime.combine(today, datetime.min.time())
    start_date = today.replace(day=1)
    next_month = (start_date.replace(day=28) + timedelta(days=4)).replace(day=1)
    end_date = next_month - timedelta(days=1)
    range_start = datetime.combine(start_date, datetime.min.time())
    range_end = datetime.combine(next_month, datetime.min.time())

    range_orders = _visible_orders_query(db).filter(
        models.Order.created_at >= range_start,
        models.Order.created_at < range_end,
    ).all()
    range_latest_tracks = _latest_track_text_by_order(db, range_orders)

    recent_orders = range_orders
    latest_tracks = _latest_track_text_by_order(db, recent_orders)
    # Every order is counted exactly once by its latest known state. Keep the
    # full lifecycle visible so a zero in one stage is not mistaken for missing
    # data (for example, an order leaves "picked" after it starts transit).
    dynamics = {
        key: 0 for key in (
            "pending", "rejected", "pending_pickup", "picked", "transit",
            "delivering", "delivered", "exception", "cancelled",
        )
    }
    for order in recent_orders:
        category = _category_with_latest_track(order, latest_tracks)
        if category in dynamics:
            dynamics[category] += 1

    month_start = datetime.combine(today.replace(day=1), datetime.min.time())
    # 上月同日
    try:
        last_month_start = (month_start - timedelta(days=1)).replace(day=1)
    except:
        last_month_start = month_start - timedelta(days=30)

    stats = {
        "total": len(range_orders),
        "pending": _visible_orders_query(db).filter(models.Order.status == "pending").count(),
        "ordered": _visible_orders_query(db).filter(models.Order.status.in_(["ordered", "success", "delivered"])).count(),
        "failed": _visible_orders_query(db).filter(models.Order.status.in_(["failed", "rejected", "cancelled"])).count(),
        "sf": sum(1 for order in range_orders if order.courier_type == "SF"),
        "sf_city": sum(1 for order in range_orders if order.courier_type == "SF_CITY"),
        "db": sum(1 for order in range_orders if order.courier_type == "DB"),
        "jdl": sum(1 for order in range_orders if order.courier_type in {"JDL", "JD"}),
        "today": _visible_orders_query(db).filter(models.Order.created_at >= ts).count(),
        "month_total": _visible_orders_query(db).filter(models.Order.created_at >= month_start).count(),
        "month_prev": _visible_orders_query(db).filter(models.Order.created_at >= last_month_start, models.Order.created_at < month_start).count(),
        "range_total": len(range_orders),
        "range_delivered": sum(
            1 for order in range_orders
            if _category_with_latest_track(order, range_latest_tracks) == "delivered"
        ),
    }
    stats["month_diff"] = stats["month_total"] - stats["month_prev"]

    # 7 天趋势 (带 max 方便模板渲染百分比)
    trend = []
    max_count = 0
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        ds = datetime.combine(d, datetime.min.time())
        de = ds + timedelta(days=1)
        c = _visible_orders_query(db).filter(
            models.Order.created_at >= ds, models.Order.created_at < de
        ).count()
        trend.append({"date": d.strftime("%m-%d"), "count": c})
        max_count = max(max_count, c)
    for t in trend:
        t["max"] = max_count or 1

    from utils.sf_city_access import sf_city_runtime_enabled
    return templates.TemplateResponse(request, name="admin.html", context={
        "stats": stats, "trend": trend, "dynamics": dynamics,
        "sf_city_feature_enabled": sf_city_runtime_enabled(db),
        "current_month": start_date.strftime("%Y-%m"),
        "current_month_label": start_date.strftime("%Y年%m月"),
    })


def _feedback_user_id(request: Request):
    current_user = request.state.current_user
    if current_user:
        return current_user["id"]
    raw = request.query_params.get("user_id") or request.headers.get("x-user-id")
    return int(raw) if raw and str(raw).isdigit() else None


@app.get("/feedback", response_class=HTMLResponse)
async def feedback_page(request: Request, db: Session = Depends(get_db)):
    user_id = _feedback_user_id(request)
    items = []
    if user_id:
        items = db.query(models.Feedback).filter(
            models.Feedback.user_id == user_id
        ).order_by(models.Feedback.created_at.desc()).all()
    return templates.TemplateResponse(
        request,
        name="feedback.html",
        context={"feedback_items": items, "current_user_id": user_id},
    )


@app.post("/api/feedback")
def create_feedback(
    body: dict, request: Request, background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    content = str(body.get("content") or "").strip()
    current_user = request.state.current_user
    raw_user_id = current_user["id"] if current_user else body.get("user_id")
    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="请先登录后再提交意见")
    if not content:
        raise HTTPException(status_code=400, detail="请填写意见内容")
    if len(content) > 1000:
        raise HTTPException(status_code=400, detail="意见内容不能超过1000个字")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="当前用户不存在")
    item = models.Feedback(user_id=user_id, content=content, status="pending")
    db.add(item)
    db.commit()
    db.refresh(item)
    notification = queue_feedback_notification(background_tasks, db, item)
    return {"ok": True, "id": item.id, "status": item.status,
            "notification": notification}


def _assert_feedback_reviewer(request: Request, db: Session):
    if not bool(getattr(request.state, "auth_enabled", False)):
        return
    current_user = getattr(request.state, "current_user", None) or {}
    if str(current_user.get("role") or "") == "admin":
        return
    reviewer_id = get_approval_policy(db).get("level2_user_id")
    if not reviewer_id or int(current_user.get("id") or 0) != int(reviewer_id):
        raise HTTPException(status_code=403, detail="仅管理员或当前配置的二级审核员可以处理意见")


@app.get("/admin/feedback", response_class=HTMLResponse)
async def admin_feedback_page(request: Request, db: Session = Depends(get_db)):
    _assert_feedback_reviewer(request, db)
    rows = db.query(models.Feedback).order_by(
        models.Feedback.status.asc(), models.Feedback.created_at.desc()
    ).all()
    user_ids = {row.user_id for row in rows}
    users = db.query(models.User).filter(models.User.id.in_(user_ids)).all() if user_ids else []
    user_map = {user.id: user for user in users}
    items = [{"feedback": row, "user": user_map.get(row.user_id)} for row in rows]
    return templates.TemplateResponse(
        request,
        name="admin_feedback.html",
        context={
            "feedback_items": items,
            "pending_count": sum(1 for row in rows if row.status == "pending"),
            "selected_feedback_id": int(request.query_params.get("feedback_id") or 0)
                if str(request.query_params.get("feedback_id") or "").isdigit() else 0,
        },
    )


@app.post("/api/admin/feedback/{feedback_id}/resolve")
def resolve_feedback(
    feedback_id: int, request: Request, db: Session = Depends(get_db)
):
    _assert_feedback_reviewer(request, db)
    item = db.query(models.Feedback).filter(models.Feedback.id == feedback_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="意见记录不存在")
    item.status = "resolved"
    item.processed_at = datetime.now()
    db.commit()
    return {"ok": True, "id": item.id, "status": item.status}


@app.get("/admin/approvals", response_class=HTMLResponse)
async def admin_approvals(request: Request, db: Session = Depends(get_db)):
    current_user = request.state.current_user
    if current_user:
        current_user_id = int(current_user["id"])
        pending_assignee = or_(
            and_(
                or_(models.Order.approval_current_level == 1,
                    models.Order.approval_current_level.is_(None)),
                models.Order.approval_level1_user_id == current_user_id,
            ),
            and_(models.Order.approval_current_level == 2,
                 models.Order.approval_level2_user_id == current_user_id),
        )
        pending_query = db.query(models.Order).filter(
            models.Order.status == "pending", models.Order.batch_id.is_(None),
        )
        pending_query = pending_query.filter(pending_assignee)
        pending = pending_query.order_by(models.Order.created_at.desc()).all()
        pending_ids = [order.id for order in pending]
        # 审批历史按实际执行审批的人读取，而不是先截取全公司的最近 50 条再过滤。
        # 一级审批完成、申请仍等待二级审批时，也应立即出现在一级审批人的历史中。
        acted_by_user = or_(
            and_(models.Order.approval_level1_user_id == current_user_id,
                 models.Order.approval_level1_time.isnot(None)),
            and_(models.Order.approval_level2_user_id == current_user_id,
                 models.Order.approval_level2_time.isnot(None)),
            and_(models.Order.approver_id == current_user_id,
                 models.Order.approve_time.isnot(None)),
        )
        history_query = db.query(models.Order).filter(
            models.Order.batch_id.is_(None), acted_by_user,
        )
        if pending_ids:
            history_query = history_query.filter(models.Order.id.notin_(pending_ids))
        history = history_query.order_by(
            models.Order.approve_time.desc(), models.Order.created_at.desc(),
        ).limit(50).all()

        pending_batch_assignee = or_(
            and_(
                or_(models.Batch.approval_current_level == 1,
                    models.Batch.approval_current_level.is_(None)),
                models.Batch.approval_level1_user_id == current_user_id,
            ),
            and_(models.Batch.approval_current_level == 2,
                 models.Batch.approval_level2_user_id == current_user_id),
        )
        pending_batch_query = db.query(models.Batch).filter(
            models.Batch.status == "pending_approval"
        )
        pending_batch_query = pending_batch_query.filter(pending_batch_assignee)
        pending_batches = pending_batch_query.order_by(models.Batch.created_at.desc()).all()
        pending_batch_ids = [batch.id for batch in pending_batches]
        batch_acted_by_user = or_(
            and_(models.Batch.approval_level1_user_id == current_user_id,
                 models.Batch.approval_level1_time.isnot(None)),
            and_(models.Batch.approval_level2_user_id == current_user_id,
                 models.Batch.approval_level2_time.isnot(None)),
            and_(models.Batch.approver_id == current_user_id,
                 models.Batch.approve_time.isnot(None)),
        )
        history_batch_query = db.query(models.Batch).filter(batch_acted_by_user)
        if pending_batch_ids:
            history_batch_query = history_batch_query.filter(
                models.Batch.id.notin_(pending_batch_ids)
            )
        history_batches = history_batch_query.order_by(
            models.Batch.approve_time.desc(), models.Batch.created_at.desc(),
        ).limit(50).all()
        pending_manual_waybills = db.query(models.ManualWaybillRecord).filter(
            models.ManualWaybillRecord.status == "pending_approval",
            models.ManualWaybillRecord.approval_level2_user_id == current_user_id,
        ).order_by(models.ManualWaybillRecord.created_at.desc()).all()
        history_manual_waybills = db.query(models.ManualWaybillRecord).filter(
            models.ManualWaybillRecord.approval_level2_user_id == current_user_id,
            models.ManualWaybillRecord.approval_level2_time.isnot(None),
            models.ManualWaybillRecord.status.in_(("approved", "rejected")),
        ).order_by(models.ManualWaybillRecord.approval_level2_time.desc()).limit(50).all()
    else:
        pending = db.query(models.Order).filter(
            models.Order.status == "pending", models.Order.batch_id.is_(None)
        ).order_by(models.Order.created_at.desc()).all()
        history = db.query(models.Order).filter(
            models.Order.status != "pending", models.Order.batch_id.is_(None),
        ).order_by(models.Order.created_at.desc()).limit(50).all()
        pending_batches = db.query(models.Batch).filter(
            models.Batch.status == "pending_approval"
        ).order_by(models.Batch.created_at.desc()).all()
        history_batches = db.query(models.Batch).filter(
            models.Batch.status.notin_(("pending_approval", "draft"))
        ).order_by(models.Batch.created_at.desc()).limit(50).all()
        pending_manual_waybills = db.query(models.ManualWaybillRecord).filter(
            models.ManualWaybillRecord.status == "pending_approval"
        ).order_by(models.ManualWaybillRecord.created_at.desc()).all()
        history_manual_waybills = db.query(models.ManualWaybillRecord).filter(
            models.ManualWaybillRecord.approval_level2_time.isnot(None),
            models.ManualWaybillRecord.status.in_(("approved", "rejected")),
        ).order_by(models.ManualWaybillRecord.approval_level2_time.desc()).limit(50).all()
    visible_batches = pending_batches + history_batches
    visible_batch_ids = [batch.id for batch in visible_batches]
    if visible_batch_ids:
        quote_totals = dict(db.query(
            models.Order.batch_id, func.sum(models.Order.freight)
        ).filter(
            models.Order.batch_id.in_(visible_batch_ids)
        ).group_by(models.Order.batch_id).all())
        for batch in visible_batches:
            batch.quote_total = quote_totals.get(batch.id)
    order_rows = pending + history
    _apply_order_display_statuses(db, order_rows)
    user_ids = {order.created_by for order in order_rows if order.created_by}
    user_ids.update(
        row.created_by for row in pending_manual_waybills + history_manual_waybills
        if row.created_by
    )
    users = db.query(models.User).filter(models.User.id.in_(user_ids)).all() if user_ids else []
    creator_map = {user.id: user.name or "未命名员工" for user in users}
    return templates.TemplateResponse(request, name="approvals.html", context={
        "pending": pending, "history": history,
        "pending_batches": pending_batches, "history_batches": history_batches,
        "pending_manual_waybills": pending_manual_waybills,
        "history_manual_waybills": history_manual_waybills,
        "creator_map": creator_map,
        "current_approval_user_id": int(current_user["id"]) if current_user else None,
    })


@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(request: Request):
    return templates.TemplateResponse(request, name="admin_users.html", context={})


@app.get("/admin/approval-settings", response_class=HTMLResponse)
async def admin_approval_settings(request: Request, db: Session = Depends(get_db)):
    users = db.query(models.User).filter(models.User.is_active.is_(True)).order_by(
        models.User.department, models.User.name
    ).all()
    level1_applicants = [user for user in users if requires_level1_approval(user)]
    return templates.TemplateResponse(request, name="admin_approval_settings.html", context={
        "users": users,
        "level1_applicants": level1_applicants,
        "level1_applicant_ids": {user.id for user in level1_applicants},
        "user_by_id": {user.id: user for user in users},
        "user_options": [
            {"id": user.id, "label": f"{user.name or '未命名员工'} · {user.department or '未设置部门'} · #{user.id}"}
            for user in users
        ],
        "policy": get_approval_policy(db),
    })


def _apply_global_approval_settings(body: dict, db: Session):
    try:
        level_count = 2
        level2_user_id = int(body.get("level2_user_id") or 0)
        reminder_interval_minutes = int(body.get("reminder_interval_minutes") or 60)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="二级审批人或催办间隔格式不正确")
    if reminder_interval_minutes not in {30, 60, 120, 240, 480, 1440, 10080}:
        raise HTTPException(status_code=400, detail="请选择有效的待审批重复提醒间隔")
    if not level2_user_id:
        raise HTTPException(status_code=400, detail="请选择固定二级审批人")
    level2_user = db.query(models.User).filter(
        models.User.id == level2_user_id, models.User.is_active.is_(True)
    ).first()
    if not level2_user:
        raise HTTPException(status_code=400, detail="所选二级审批人不存在或账号已停用")
    level1_enabled = bool(body.get("level1_enabled"))
    save_config(db, LEVEL_COUNT_KEY, level_count, "审批流程级数")
    save_config(db, LEVEL1_ENABLED_KEY, "1" if level1_enabled else "0", "是否启用一级审批")
    save_config(db, LEVEL2_USER_KEY, level2_user_id, "二级固定审批人")
    save_config(
        db, REMINDER_INTERVAL_MINUTES_KEY, reminder_interval_minutes,
        "待审批工作通知重复提醒间隔（分钟）",
    )
    save_config(db, "approval_required", "1", "是否需要审批")


def _apply_supervisor_approval_settings(body: dict, db: Session):
    supervisor_payload = body.get("supervisors") or {}
    if not isinstance(supervisor_payload, dict):
        raise HTTPException(status_code=400, detail="一级审批人配置格式不正确")
    supervisor_map = {}
    submitted_applicant_ids = set()
    try:
        for applicant_id, supervisor_id in supervisor_payload.items():
            applicant_id = int(applicant_id)
            supervisor_id = int(supervisor_id or 0)
            submitted_applicant_ids.add(applicant_id)
            if supervisor_id:
                if applicant_id == supervisor_id:
                    raise HTTPException(status_code=400, detail="员工不能设置自己为一级审批人")
                supervisor_map[applicant_id] = supervisor_id
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="一级审批人配置包含无效员工")
    active_users = db.query(models.User).filter(models.User.is_active.is_(True)).all()
    active_ids = {user.id for user in active_users}
    if submitted_applicant_ids != active_ids:
        raise HTTPException(status_code=400, detail="员工审批配置不完整，请刷新页面后重新修改")
    required_ids = {*supervisor_map.keys(), *supervisor_map.values()}
    selected = db.query(models.User).filter(
        models.User.id.in_(required_ids), models.User.is_active.is_(True)
    ).all()
    if len(selected) != len(required_ids):
        raise HTTPException(status_code=400, detail="所选审批人不存在或账号已停用")
    for applicant in active_users:
        supervisor_id = supervisor_map.get(applicant.id)
        if supervisor_id:
            applicant.supervisor_user_id = supervisor_id
            applicant.approval_level1_required_override = True
        else:
            applicant.approval_level1_required_override = False


def _refresh_pending_approval_records(db: Session):
    pending_orders = db.query(models.Order).filter(models.Order.status == "pending").all()
    pending_batches = db.query(models.Batch).filter(
        models.Batch.status == "pending_approval"
    ).all()
    for record in [*pending_orders, *pending_batches]:
        refresh_pending_approval(record, db)
    return len(pending_orders) + len(pending_batches)


def _approval_settings_response(db: Session, updated_pending: int):
    return {
        "ok": True,
        "policy": get_approval_policy(db),
        "updated_pending": updated_pending,
    }


@app.post("/api/admin/approval-settings/global")
def save_global_approval_settings(body: dict, db: Session = Depends(get_db)):
    _apply_global_approval_settings(body, db)
    updated_pending = _refresh_pending_approval_records(db)
    db.commit()
    return _approval_settings_response(db, updated_pending)


@app.post("/api/admin/approval-settings/supervisors")
def save_supervisor_approval_settings(body: dict, db: Session = Depends(get_db)):
    _apply_supervisor_approval_settings(body, db)
    updated_pending = _refresh_pending_approval_records(db)
    db.commit()
    return _approval_settings_response(db, updated_pending)


@app.post("/api/admin/approval-settings")
def save_approval_settings(body: dict, db: Session = Depends(get_db)):
    """Backward-compatible endpoint for older deployed pages and API clients."""
    _apply_global_approval_settings(body, db)
    _apply_supervisor_approval_settings(body, db)
    updated_pending = _refresh_pending_approval_records(db)
    db.commit()
    return _approval_settings_response(db, updated_pending)


@app.get("/admin/item-types", response_class=HTMLResponse)
async def admin_item_types(request: Request, db: Session = Depends(get_db)):
    from utils.regions import REGIONS, available_services, default_service_id

    courier_labels = {"SF": "顺丰", "DB": "德邦", "JDL": "京东"}
    service_settings = []
    for region_code, region_label in REGIONS:
        services = available_services(db, region_code)
        service_settings.append({
            "region_code": region_code,
            "region_label": region_label,
            "default_service_id": default_service_id(db, region_code),
            "services": [{
                "id": service["id"],
                "label": f"{courier_labels.get(service['courier'], service['courier'])} · {service['name']}",
            } for service in services],
        })
    return templates.TemplateResponse(
        request,
        name="admin_item_types.html",
        context={"item_types": _get_item_types(db), "service_settings": service_settings},
    )


@app.post("/api/admin/item-types")
def save_admin_item_types(body: dict, db: Session = Depends(get_db)):
    raw_values = body.get("item_types")
    if not isinstance(raw_values, list):
        raise HTTPException(status_code=400, detail="物品类型必须是列表")

    values = []
    for raw_value in raw_values:
        value = str(raw_value).strip()
        if not value or value in values:
            continue
        if len(value) > 20:
            raise HTTPException(status_code=400, detail="单个物品类型不能超过20个字")
        values.append(value)

    if not values:
        raise HTTPException(status_code=400, detail="至少保留一个物品类型")
    if len(values) > 15:
        raise HTTPException(status_code=400, detail="物品类型最多15项")

    row = db.query(models.SystemConfig).filter(
        models.SystemConfig.config_key == "item_types"
    ).first()
    serialized = json.dumps(values, ensure_ascii=False)
    if row:
        row.config_value = serialized
    else:
        db.add(models.SystemConfig(
            config_key="item_types",
            config_value=serialized,
            description="员工下单可选物品类型",
        ))
    db.commit()
    return {"ok": True, "item_types": values, "count": len(values)}


@app.post("/api/admin/default-service")
def save_admin_default_service(body: dict, db: Session = Depends(get_db)):
    from utils.regions import normalize_region, save_default_service_id

    region_code = normalize_region(body.get("region_code"))
    try:
        save_default_service_id(db, region_code, body.get("service_id"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return {"ok": True, "region_code": region_code, "service_id": body.get("service_id")}

# ============ 收件人智能解析 ============
from utils.address_parser import parse_recipient

@app.post("/api/parse-recipient")
async def api_parse_recipient(body: dict):
    text = body.get("text", "")
    result = parse_recipient(text)
    # 直辖市补市名
    if result.get("province") and not result.get("city"):
        zx = ["北京市","天津市","上海市","重庆市"]
        if result["province"] in zx:
            result["city"] = result["province"]
    return result


# ============ 快递服务目录 ============
from courier_services import ALL_SERVICES, available_in_region, service_display_name


@app.get("/api/services")
def api_services(
    request: Request, db: Session = Depends(get_db), courier: str = None,
    region: str = None,
):
    from utils.regions import default_service_id, enabled_carriers, request_region
    region_code = request_region(
        db, request.state.current_user, region,
        auth_enabled=bool(getattr(request.state, "auth_enabled", False)),
    )
    allowed_carriers = set(enabled_carriers(db, region_code))
    items = []
    for s in ALL_SERVICES:
        if courier and s["courier"] != courier.upper():
            continue
        if s["courier"] not in allowed_carriers:
            continue
        if not available_in_region(s, region_code):
            continue
        items.append({
            "id": s["id"],
            "courier": s["courier"],
            "name": s["name"],
            "express_type": s["express_type"],
            "tagline": s["tagline"],
            "difference": s["difference"],
            "weight_min": s["weight_min"],
            "weight_max": s["weight_max"],
            "transit_time": s["transit_time"],
            "speed_level": s["speed_level"],
            "size_class": s["size_class"],
            "supports_insurance": s["courier"] in {"SF", "DB", "JDL"},
            "supports_multi_parcel": s["courier"] in {"SF", "JDL"},
            "enabled": True,
        })
    selected_default = default_service_id(db, region_code)
    default_row = next((item for item in items if item["id"] == selected_default), None)
    return {
        "ok": True, "items": items, "region_code": region_code,
        "default_service_id": selected_default,
        "default_service_name": service_display_name(default_row),
    }




@app.get("/admin/cost", response_class=HTMLResponse)
def admin_cost_page(request: Request):
    # The read-only page loads both current CRM reference lists through the
    # existing APIs so its two fixed-height tables can search instantly.
    return templates.TemplateResponse(request, name="admin_cost.html", context={})
# ============ 月结账号 / 系统设置 ============
@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings_page(request: Request, db: Session = Depends(get_db)):
    from config.settings import settings as runtime_settings
    from utils.regions import enabled_carriers, get_config, normalize_region, region_options
    from utils.sf_city_access import sf_city_runtime_enabled

    region = normalize_region(request.query_params.get("region"))

    def get_cfg(key, default=""):
        value = get_config(db, region, key, "")
        if value or request.query_params.get("region") is not None:
            return value
        legacy = db.query(models.SystemConfig).filter(models.SystemConfig.config_key == key).first()
        return legacy.config_value if legacy else default

    page_settings = {
        "sf_city_feature_enabled": sf_city_runtime_enabled(db),
        "sf_customer_code": get_cfg("sf_customer_code", runtime_settings.sf_customer_code),
        "sf_check_word": get_cfg("sf_check_word", runtime_settings.sf_check_word),
        "sf_monthly_card": get_cfg("sf_monthly_card", runtime_settings.sf_monthly_card),
        "sf_city_dev_id": get_cfg("sf_city_dev_id", runtime_settings.sf_city_dev_id),
        "sf_city_dev_key": get_cfg("sf_city_dev_key", runtime_settings.sf_city_dev_key),
        "sf_city_shop_id": get_cfg("sf_city_shop_id", runtime_settings.sf_city_shop_id),
        "sf_city_live_order_enabled": get_cfg(
            "sf_city_live_order_enabled",
            "1" if runtime_settings.sf_city_live_order_enabled else "0",
        ),
        "db_company_code": get_cfg("db_company_code", runtime_settings.db_company_code),
        "db_appkey": get_cfg("db_appkey", runtime_settings.db_appkey),
        "db_sign": get_cfg("db_sign", runtime_settings.db_sign),
        "db_customer_code": get_cfg("db_customer_code", runtime_settings.db_customer_code),
        "jdl_app_key": get_cfg("jdl_app_key", runtime_settings.jdl_app_key),
        "jdl_app_secret": get_cfg("jdl_app_secret", runtime_settings.jdl_app_secret),
        "jdl_access_token": get_cfg("jdl_access_token", runtime_settings.jdl_access_token),
        "jdl_customer_code": get_cfg("jdl_customer_code", runtime_settings.jdl_customer_code),
        "jdl_business_unit_code": get_cfg(
            "jdl_business_unit_code", runtime_settings.jdl_business_unit_code
        ),
    }
    return templates.TemplateResponse(request, name="admin_settings.html", context={
        "settings": page_settings, "regions": region_options(), "selected_region": region,
        "enabled_carriers": enabled_carriers(db, region),
    })


@app.post("/api/admin/settings")
def save_admin_settings(body: dict, db: Session = Depends(get_db)):
    from utils.regions import normalize_region, save_config as save_region_config, save_enabled_carriers
    region = normalize_region(body.get("region_code"))
    legacy_request = "region_code" not in body
    allowed_keys = {
        "sf_customer_code": "顺丰顾客编码",
        "sf_monthly_card": "顺丰月结卡号",
        "sf_city_dev_id": "顺丰同城开发者 ID",
        "sf_city_dev_key": "顺丰同城开发者密钥",
        "sf_city_shop_id": "顺丰同城店铺 ID",
        "sf_city_live_order_enabled": "顺丰同城真实下单开关",
        "db_customer_code": "德邦客户编码",
        "db_company_code": "德邦公司编码",
        "db_sign": "德邦 sign 值",
        "jdl_app_key": "京东物流 AppKey",
        "jdl_app_secret": "京东物流 AppSecret",
        "jdl_access_token": "京东物流 Access Token",
        "jdl_customer_code": "京东物流月结编码",
        "jdl_business_unit_code": "京东物流快运事业部编码",
    }
    saved = []
    for key, label in allowed_keys.items():
        if key in body:
            val = str(body[key])
            save_region_config(db, region, key, val, label)
            if legacy_request:
                row = db.query(models.SystemConfig).filter(models.SystemConfig.config_key == key).first()
                if row: row.config_value = val
                else: db.add(models.SystemConfig(config_key=key, config_value=val, description=label))
            saved.append(key)
    # 生产校验码和 appkey 在管理员配置页直接显示并可修改。
    check_word = str(body.get("sf_check_word") or "").strip()
    if "sf_check_word" in body:
        save_region_config(db, region, "sf_check_word", check_word, "顺丰生产校验码")
        if legacy_request:
            row = db.query(models.SystemConfig).filter(models.SystemConfig.config_key == "sf_check_word").first()
            if row: row.config_value = check_word
            else: db.add(models.SystemConfig(config_key="sf_check_word", config_value=check_word, description="顺丰生产校验码"))
        saved.append("sf_check_word")
    db_appkey = str(body.get("db_appkey") or "").strip()
    if "db_appkey" in body:
        save_region_config(db, region, "db_appkey", db_appkey, "德邦 appkey")
        if legacy_request:
            row = db.query(models.SystemConfig).filter(models.SystemConfig.config_key == "db_appkey").first()
            if row: row.config_value = db_appkey
            else: db.add(models.SystemConfig(config_key="db_appkey", config_value=db_appkey, description="德邦 appkey"))
        saved.append("db_appkey")
    if "enabled_carriers" in body:
        save_enabled_carriers(db, region, body.get("enabled_carriers") or [])
    # 所有订单固定进入审批流程，不向配置页面提供关闭审批的选项。
    approval_row = db.query(models.SystemConfig).filter(
        models.SystemConfig.config_key == "approval_required"
    ).first()
    if approval_row:
        approval_row.config_value = "1"
    else:
        db.add(models.SystemConfig(
            config_key="approval_required", config_value="1", description="是否需要审批"
        ))
    db.commit()
    return {"ok": True, "saved": saved}
