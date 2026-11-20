"""Generate, persist and notify administrators about monthly carrier exports."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
import os
import hashlib
from pathlib import Path
import re

from config.settings import settings
from database import models
from database.connection import SessionLocal
from dingtalk import DingTalkClient
from utils.approval_notifications import dingtalk_notification_url
from utils.order_export import build_order_export
from utils.order_status import ORDER_STATUS_LABELS, classify_order_status


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
CARRIERS = {"SF": "顺丰", "DB": "德邦", "JDL": "京东"}
REGION_NAMES = {"all": "全部区域", "xiamen_other": "厦门及其他", "chengdu": "成都", "beijing": "北京"}
STATUS_LABELS = ORDER_STATUS_LABELS
EXPORT_SCHEMA_VERSION = 2



def _export_root(output_dir=None):
    path = Path(output_dir or settings.monthly_export_dir)
    return path if path.is_absolute() else ROOT / path


def _month_bounds(month):
    if isinstance(month, str):
        if not MONTH_PATTERN.fullmatch(month):
            raise ValueError("月份格式必须为 YYYY-MM")
        start = datetime.strptime(month, "%Y-%m")
    else:
        start = datetime(month.year, month.month, 1)
    next_month = datetime(start.year + (start.month == 12), (start.month % 12) + 1, 1)
    return start, next_month


def previous_month(now=None):
    current = now or datetime.now()
    return (datetime(current.year, current.month, 1) - timedelta(days=1)).strftime("%Y-%m")


def _order_category(order):
    return classify_order_status(order.status, order.track_status, bool(order.courier_order_no))


def _orders_for_month(db, month, courier_type, region_code="all"):
    start, next_month = _month_bounds(month)
    query = db.query(models.Order).filter(
        models.Order.created_at >= start,
        models.Order.created_at < next_month,
        models.Order.courier_type == courier_type,
    )
    if region_code != "all":
        query = query.filter(models.Order.region_code == region_code)
    orders = query.order_by(models.Order.created_at.asc()).all()
    order_ids = [order.id for order in orders]
    if order_ids:
        tracks = db.query(models.CourierTrack).filter(
            models.CourierTrack.order_id.in_(order_ids)
        ).order_by(
            models.CourierTrack.order_id,
            models.CourierTrack.track_time.desc(),
            models.CourierTrack.id.desc(),
        ).all()
        latest = {}
        for track in tracks:
            latest.setdefault(track.order_id, track.track_status or track.track_detail or "")
        for order in orders:
            if latest.get(order.id):
                order.track_status = latest[order.id]
    return orders


def _creator_details(db, orders):
    user_ids = {order.created_by for order in orders if order.created_by}
    users = db.query(models.User).filter(models.User.id.in_(user_ids)).all() if user_ids else []
    return {
        user.id: {"name": user.name or "未命名员工", "department": user.department or ""}
        for user in users
    }


def _atomic_bytes(path, value):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_bytes(value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_manifest(path, data):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _manifest_path(month, output_dir=None):
    return _export_root(output_dir) / month / "manifest.json"


def load_manifest(month, output_dir=None):
    if not MONTH_PATTERN.fullmatch(str(month or "")):
        return None
    path = _manifest_path(month, output_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _manifest_files_exist(manifest, output_dir=None):
    month = str((manifest or {}).get("month") or "")
    if not MONTH_PATTERN.fullmatch(month):
        return False
    if int((manifest or {}).get("schema_version") or 0) != EXPORT_SCHEMA_VERSION:
        return False
    directory = _export_root(output_dir) / month
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    required = [f"{region}-{code}" for region in REGION_NAMES for code in CARRIERS]
    return all(
        isinstance(files.get(key), dict)
        and (directory / str(files[key].get("filename") or "")).is_file()
        for key in required
    )


def generate_monthly_exports(db, month, output_dir=None):
    """Generate carrier workbooks for every region and an all-region view."""
    start, next_month = _month_bounds(month)
    month = start.strftime("%Y-%m")
    directory = _export_root(output_dir) / month
    directory.mkdir(parents=True, exist_ok=True)
    files = {}
    for region_code, region_name in REGION_NAMES.items():
        for courier_type, carrier_name in CARRIERS.items():
            orders = _orders_for_month(db, month, courier_type, region_code)
            stream = build_order_export(
                orders, _creator_details(db, orders), _order_category, STATUS_LABELS,
                region_name=region_name, carrier_name=carrier_name,
            )
            filename = (
                f"{carrier_name}寄件详情-{start:%Y年%m月}.xlsx"
                if region_code == "all"
                else f"{region_name}-{carrier_name}寄件详情-{start:%Y年%m月}.xlsx"
            )
            _atomic_bytes(directory / filename, stream.getvalue())
            files[f"{region_code}-{courier_type}"] = {"filename": filename, "count": len(orders)}
            if region_code == "all":
                files[courier_type] = files[f"{region_code}-{courier_type}"]
    previous = load_manifest(month, output_dir) or {}
    previous_notification = previous.get("notification")
    if not isinstance(previous_notification, dict):
        previous_notification = {"sent": False, "attempts": 0}
    manifest = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "month": month,
        "period_start": start.date().isoformat(),
        "period_end": (next_month - timedelta(days=1)).date().isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "files": files,
        # 文件缺失而重建时保留已发送状态，避免仅因文件修复再次通知管理员。
        "notification": previous_notification,
    }
    _write_manifest(directory / "manifest.json", manifest)
    return manifest


def _recipient_key(dingtalk_userid):
    return hashlib.sha256(str(dingtalk_userid).encode("utf-8")).hexdigest()


def _notify_admins(db, manifest, sent_recipient_keys=None, on_sent=None):
    admins = db.query(models.User).filter(
        models.User.role.in_(("admin", "admin_approver")),
        models.User.is_active.isnot(False),
        models.User.dingtalk_userid.isnot(None),
        models.User.dingtalk_userid != "",
    ).order_by(models.User.id).all()
    recipients = []
    seen = set()
    for user in admins:
        userid = str(user.dingtalk_userid or "").strip()
        if userid and userid not in seen:
            recipients.append((user, userid))
            seen.add(userid)
    if not recipients:
        raise RuntimeError("没有可接收通知的启用管理员")
    base_url = str(settings.public_base_url or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("PUBLIC_BASE_URL 未配置")
    month = manifest["month"]
    shown_month = datetime.strptime(month, "%Y-%m").strftime("%Y年%m月")
    sf_count = manifest["files"]["all-SF"]["count"]
    db_count = manifest["files"]["all-DB"]["count"]
    jdl_count = manifest["files"]["all-JDL"]["count"]
    client = DingTalkClient()
    sent_keys = set(sent_recipient_keys or [])
    responses = []
    for user, userid in recipients:
        key = _recipient_key(userid)
        if key in sent_keys:
            continue
        target = dingtalk_notification_url(
            user, f"/admin/monthly-exports?month={month}&region=xiamen_other"
        )
        response = client.send_work_notification(
            userid,
            "快递费用分摊提醒",
            f"### {shown_month}快递费用分摊待处理\n\n"
            f"系统寄件记录已整理：顺丰 {sf_count} 条、德邦 {db_count} 条、"
            f"京东 {jdl_count} 条。\n\n"
            "请按区域上传承运商提供的实际费用明细，核对未匹配运单后下载"
            "部门分摊、项目分摊和分摊明细。",
            "进入费用分摊",
            target,
        )
        responses.append(response)
        sent_keys.add(key)
        if on_sent:
            on_sent(key, response)
    return responses, len(recipients), sorted(sent_keys)


def ensure_monthly_exports(month=None, now=None, db=None, output_dir=None):
    """Generate once and retry a failed notification without sending duplicates."""
    month = month or previous_month(now)
    own_db = db is None
    db = db or SessionLocal()
    try:
        manifest = load_manifest(month, output_dir)
        if not _manifest_files_exist(manifest, output_dir):
            manifest = generate_monthly_exports(db, month, output_dir)
        notification = manifest.setdefault("notification", {})
        if notification.get("sent") or notification.get("suppressed"):
            return manifest
        notification["attempts"] = int(notification.get("attempts") or 0) + 1
        notification["last_attempt_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            sent_keys = set(notification.get("sent_recipient_keys") or [])

            def persist_recipient(key, response):
                sent_keys.add(key)
                notification["sent_recipient_keys"] = sorted(sent_keys)
                task_id = response.get("task_id")
                if task_id is not None:
                    task_ids = notification.setdefault("task_ids", [])
                    if task_id not in task_ids:
                        task_ids.append(task_id)
                _write_manifest(_manifest_path(month, output_dir), manifest)

            responses, recipient_count, completed_keys = _notify_admins(
                db, manifest, sent_keys, persist_recipient
            )
            notification.update({
                "sent": True,
                "sent_at": datetime.now().isoformat(timespec="seconds"),
                "recipient_count": recipient_count,
                "sent_recipient_keys": completed_keys,
            })
            notification.pop("error", None)
        except Exception as exc:
            notification["sent"] = False
            notification["error"] = str(exc)
            logger.exception("Monthly export notification failed: %s", exc)
        _write_manifest(_manifest_path(month, output_dir), manifest)
        return manifest
    finally:
        if own_db:
            db.close()


def backfill_monthly_export_files(month=None, now=None, db=None, output_dir=None):
    """Silently rebuild missing monthly files without sending a work notice.

    This is safe to run on every service start. It restores files after a new
    deployment or an empty runtime directory, while leaving the scheduled
    notification path exclusively to ``ensure_monthly_exports``.
    """
    month = month or previous_month(now)
    own_db = db is None
    db = db or SessionLocal()
    try:
        manifest = load_manifest(month, output_dir)
        if _manifest_files_exist(manifest, output_dir):
            return manifest
        had_manifest = bool(manifest)
        manifest = generate_monthly_exports(db, month, output_dir)
        notification = manifest.setdefault("notification", {})
        # A history file repaired on startup must never become a delayed notice
        # during a future catch-up run. Preserve a genuine prior notification
        # attempt, but explicitly suppress a brand-new backfill notification.
        if not had_manifest:
            notification.update({
                "sent": False,
                "suppressed": True,
                "suppressed_at": datetime.now().isoformat(timespec="seconds"),
                "reason": "startup_backfill",
            })
            _write_manifest(_manifest_path(month, output_dir), manifest)
        return manifest
    finally:
        if own_db:
            db.close()


def run_monthly_export_job():
    target = previous_month()
    existing = list_monthly_exports()
    months = [target]
    if existing:
        cursor, _ = _month_bounds(existing[0]["month"])
        target_start, _ = _month_bounds(target)
        months = []
        while cursor <= target_start:
            months.append(cursor.strftime("%Y-%m"))
            cursor = _month_bounds(cursor)[1]
    for month in months:
        manifest = ensure_monthly_exports(month=month)
        note = manifest.get("notification") or {}
        print(
            f"[monthly_exports] month={manifest.get('month')} "
            f"sf={manifest['files']['all-SF']['count']} db={manifest['files']['all-DB']['count']} "
            f"jdl={manifest['files']['all-JDL']['count']} "
            f"notified={bool(note.get('sent'))}"
        )


def list_monthly_exports(output_dir=None):
    root = _export_root(output_dir)
    if not root.is_dir():
        return []
    manifests = []
    for directory in root.iterdir():
        if not directory.is_dir() or not MONTH_PATTERN.fullmatch(directory.name):
            continue
        manifest = load_manifest(directory.name, output_dir)
        if manifest and _manifest_files_exist(manifest, output_dir):
            manifests.append(manifest)
    return sorted(manifests, key=lambda item: item["month"], reverse=True)


def monthly_export_path(month, courier_type, output_dir=None, region_code="all"):
    courier_type = str(courier_type or "").upper()
    region_code = str(region_code or "all").lower()
    manifest = load_manifest(month, output_dir)
    key = f"{region_code}-{courier_type}"
    if not manifest or courier_type not in CARRIERS or region_code not in REGION_NAMES or not _manifest_files_exist(manifest, output_dir):
        return None, None
    filename = manifest["files"][key]["filename"]
    if Path(filename).name != filename:
        return None, None
    return _export_root(output_dir) / month / filename, filename
