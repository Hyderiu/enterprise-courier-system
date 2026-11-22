import datetime as dt

import time


def sync_crm_reference_data():
    """每周一 09:30 从 CRM 统一刷新全部部门和项目。"""
    from api.routes.cost import _crm_sync_snapshot, _run_crm_reference_sync

    if _crm_sync_snapshot().get("status") == "running":
        print("[sync_crm_reference_data] skipped: manual sync is already running")
        return
    _run_crm_reference_sync()
    state = _crm_sync_snapshot()
    stats = state.get("stats") if isinstance(state.get("stats"), dict) else {}
    if state.get("status") == "done":
        print(
            f"[sync_crm_reference_data] departments={stats.get('department_count', 0)} "
            f"projects={stats.get('project_count', 0)} "
            f"missing_department={stats.get('projects_without_department', 0)}"
        )
    else:
        print(f"[sync_crm_reference_data] failed: {state.get('message') or 'unknown error'}")


def sync_dingtalk_directory():
    """Refresh basic directory fields and persist only new or changed employees."""
    from api.routes.cost import (
        _department_sync_snapshot, _run_department_sync,
        _set_department_sync_state,
    )

    if _department_sync_snapshot().get("status") == "running":
        print("[sync_dingtalk_directory] skipped employees: manual sync is already running")
    else:
        _set_department_sync_state(
            status="running", phase="starting", message="每周钉钉员工差异同步正在启动",
            stats=None, started_at=time.time(), finished_at=None,
        )
        _run_department_sync()
        state = _department_sync_snapshot()
        stats = state.get("stats") if isinstance(state.get("stats"), dict) else {}
        users = stats.get("users_synced") if isinstance(stats.get("users_synced"), dict) else {}
        print(
            f"[sync_dingtalk_directory] status={state.get('status')} "
            f"members={users.get('total', 0)} created={users.get('created', 0)} "
            f"updated={users.get('updated', 0)} unchanged={users.get('unchanged', 0)}"
        )


def sync_reference_data():
    """Run the Monday 09:30 CRM refresh, then the DingTalk employee delta sync."""
    sync_crm_reference_data()
    sync_dingtalk_directory()


def run_monthly_exports():
    """Generate the previous calendar month's carrier files and notify admins."""
    from utils.monthly_exports import run_monthly_export_job
    run_monthly_export_job()


def backfill_monthly_exports():
    """Restore missing previous-month files on startup without notifying users."""
    from utils.monthly_exports import backfill_monthly_export_files

    manifest = backfill_monthly_export_files()
    files = manifest.get("files") or {}
    print(
        f"[monthly_exports_backfill] month={manifest.get('month')} "
        f"sf={(files.get('all-SF') or {}).get('count', 0)} "
        f"db={(files.get('all-DB') or {}).get('count', 0)} "
        f"jdl={(files.get('all-JDL') or {}).get('count', 0)} notified=false"
    )

def poll_tracking():
    from database.connection import SessionLocal
    from database import models
    from api.routes.orders import _refresh_order_tracking
    from utils.approval_notifications import (
        send_sf_city_status_notification_once, sf_city_event_from_transition,
    )

    db = SessionLocal()
    refreshed = failed = delivered = 0
    try:
        # 优先刷新从未查过或最久未更新的订单，避免固定前50单造成饥饿。
        pending = db.query(models.Order).filter(
            models.Order.status.in_(["ordered", "success", "failed"]),
            models.Order.courier_order_no.isnot(None),
        ).order_by(
            models.Order.track_update_time.is_(None).desc(),
            models.Order.track_update_time.asc(),
            models.Order.id.asc(),
        ).limit(100).all()
        for order in pending:
            try:
                previous_rider = order.courier_picked_by
                previous_track_status = order.track_status
                result = _refresh_order_tracking(db, order, include_freight=True)
                db.commit()
                lifecycle_event = sf_city_event_from_transition(
                    order, result["previous_status"], previous_rider,
                    previous_track_status,
                )
                if lifecycle_event:
                    send_sf_city_status_notification_once(
                        db, order, lifecycle_event, order.track_status or ""
                    )
                refreshed += 1
                if result["previous_status"] != "delivered" and result["order_status"] == "delivered":
                    delivered += 1
            except Exception as exc:
                db.rollback()
                failed += 1
                print(f"[poll_tracking] order={order.id} failed: {exc}")
        print(
            f"[poll_tracking] total={len(pending)} refreshed={refreshed} "
            f"delivered={delivered} failed={failed}"
        )
    except Exception as exc:
        db.rollback()
        print(f"[poll_tracking] failed: {exc}")
    finally:
        db.close()


def cancel_stale_sf_city_orders(now=None):
    """Cancel SF City orders that still have no rider after the configured wait."""
    from config.settings import settings
    from courier import get_courier_for_order
    from database.connection import SessionLocal
    from database import models
    from api.routes.orders import _refresh_order_tracking
    from utils.approval_notifications import (
        send_sf_city_status_notification_once, sf_city_event_from_transition,
    )
    from utils.sf_city_status import is_waiting_for_rider

    current_time = now or models.china_now()
    wait_minutes = max(1, int(settings.sf_city_rider_wait_minutes or 30))
    cutoff = current_time - dt.timedelta(minutes=wait_minutes)
    db = SessionLocal()
    checked = cancelled = state_changed = failed = 0
    try:
        candidates = db.query(models.Order).filter(
            models.Order.courier_type == "SF_CITY",
            models.Order.status == "ordered",
            models.Order.courier_order_no.isnot(None),
            models.Order.order_time.isnot(None),
            models.Order.order_time <= cutoff,
        ).all()
        for order in candidates:
            checked += 1
            try:
                previous_status = order.status
                previous_rider = order.courier_picked_by
                previous_track = order.track_status
                _refresh_order_tracking(db, order, include_freight=False)
                lifecycle_event = sf_city_event_from_transition(
                    order, previous_status, previous_rider, previous_track
                )
                if not is_waiting_for_rider(order):
                    db.commit()
                    if lifecycle_event:
                        send_sf_city_status_notification_once(
                            db, order, lifecycle_event, order.track_status or ""
                        )
                    state_changed += 1
                    continue

                result = get_courier_for_order(order, db).cancel_order(
                    order.courier_order_no, order.order_no
                )
                if not result.get("success"):
                    raise RuntimeError(result.get("message") or "顺丰同城自动取消失败")
                order.status = "cancelled"
                order.track_status = f"{wait_minutes}分钟无人接单，订单已自动取消"
                order.track_update_time = current_time
                db.add(models.CourierTrack(
                    order_id=order.id,
                    courier_order_no=order.courier_order_no,
                    track_time=current_time,
                    track_status="订单已取消",
                    track_detail=order.track_status,
                ))
                db.commit()
                send_sf_city_status_notification_once(
                    db, order, "cancelled", order.track_status
                )
                cancelled += 1
            except Exception as exc:
                db.rollback()
                failed += 1
                print(f"[sf_city_auto_cancel] order={order.id} failed: {exc}")
        print(
            f"[sf_city_auto_cancel] wait={wait_minutes}m checked={checked} "
            f"cancelled={cancelled} state_changed={state_changed} failed={failed}"
        )
        return {
            "wait_minutes": wait_minutes, "checked": checked,
            "cancelled": cancelled, "state_changed": state_changed,
            "failed": failed,
        }
    finally:
        db.close()


def send_pending_approval_reminders(now=None):
    """Repeat DingTalk reminders for the active approval stage until it ends."""
    from database.connection import SessionLocal
    from database import models
    from utils.approval_notifications import (
        build_approval_notification, send_approval_notification,
    )
    from utils.approval_policy import get_approval_policy

    db = SessionLocal()
    checked = due = sent = failed = 0
    current_time = now or models.china_now()
    try:
        interval_minutes = int(
            get_approval_policy(db).get("reminder_interval_minutes") or 60
        )
        cutoff = current_time - dt.timedelta(minutes=interval_minutes)
        candidates = [
            *[(record, "order") for record in db.query(models.Order).filter(
                models.Order.status == "pending",
                models.Order.batch_id.is_(None),
            ).all()],
            *[(record, "batch") for record in db.query(models.Batch).filter(
                models.Batch.status == "pending_approval",
            ).all()],
            *[(record, "manual") for record in db.query(models.ManualWaybillRecord).filter(
                models.ManualWaybillRecord.status == "pending_approval",
            ).all()],
        ]
        for record, record_type in candidates:
            checked += 1
            stage_started = getattr(record, "approval_stage_started_at", None)
            if not stage_started and record_type != "manual":
                level = int(getattr(record, "approval_current_level", None) or 1)
                if level == 2:
                    stage_started = getattr(record, "approval_level1_time", None)
            stage_started = stage_started or getattr(record, "created_at", None)
            last_reminded = getattr(record, "approval_last_reminded_at", None)
            due_from = last_reminded or stage_started
            if not due_from or due_from > cutoff:
                continue
            due += 1
            job, reason = build_approval_notification(
                db, record, record_type, reminder=True
            )
            if not job:
                failed += 1
                print(
                    f"[approval_reminder] {record_type}={record.id} skipped: {reason}"
                )
                continue
            result = send_approval_notification(job)
            if result.get("sent"):
                record.approval_last_reminded_at = current_time
                db.commit()
                sent += 1
            else:
                db.rollback()
                failed += 1
        print(
            f"[approval_reminder] interval={interval_minutes}m checked={checked} "
            f"due={due} sent={sent} failed={failed}"
        )
        return {
            "interval_minutes": interval_minutes, "checked": checked,
            "due": due, "sent": sent, "failed": failed,
        }
    except Exception as exc:
        db.rollback()
        print(f"[approval_reminder] failed: {exc}")
        return {"checked": checked, "due": due, "sent": sent, "failed": failed + 1}
    finally:
        db.close()
