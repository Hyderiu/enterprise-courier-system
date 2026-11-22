"""Configurable, snapshot-based approval policy helpers."""
import datetime as dt

from fastapi import HTTPException

from database import models


LEVEL_COUNT_KEY = "approval_level_count"
LEVEL1_USER_KEY = "approval_level1_user_id"
LEVEL2_USER_KEY = "approval_level2_user_id"
LEVEL1_ENABLED_KEY = "approval_level1_enabled"
REMINDER_INTERVAL_MINUTES_KEY = "approval_reminder_interval_minutes"
LEVEL1_HRM_JOB_TITLES = {"职员", "3级部门负责人", "4级部门负责人"}


def default_requires_level1_approval(user):
    """Return the Smart HR default before an administrator override."""
    return bool(user) and str(user.hrm_job_title or "").strip() in LEVEL1_HRM_JOB_TITLES


def requires_level1_approval(user):
    """Return the effective per-user level-one rule, including manual overrides."""
    if not user:
        return False
    override = getattr(user, "approval_level1_required_override", None)
    if override is not None:
        return bool(override)
    return default_requires_level1_approval(user)


def _config_value(db, key, default=""):
    row = db.query(models.SystemConfig).filter(models.SystemConfig.config_key == key).first()
    return row.config_value if row and row.config_value is not None else default


def get_approval_policy(db, applicant_user_id=None):
    """Return the current global policy. Legacy/unconfigured databases remain one-step."""
    try:
        level_count = int(_config_value(db, LEVEL_COUNT_KEY, "1"))
    except (TypeError, ValueError):
        level_count = 1
    level_count = 2 if level_count == 2 else 1

    def user_id(key):
        try:
            value = int(_config_value(db, key, "0"))
            return value or None
        except (TypeError, ValueError):
            return None

    level1_enabled = str(_config_value(db, LEVEL1_ENABLED_KEY, "1")).lower() in {
        "1", "true", "yes", "on"
    }
    try:
        reminder_interval_minutes = int(
            _config_value(db, REMINDER_INTERVAL_MINUTES_KEY, "60")
        )
    except (TypeError, ValueError):
        reminder_interval_minutes = 60
    reminder_interval_minutes = max(5, min(reminder_interval_minutes, 10080))
    applicant = None
    supervisor_user_id = None
    applicant_job_title = None
    if applicant_user_id:
        applicant = db.query(models.User).filter(models.User.id == applicant_user_id).first()
        supervisor_user_id = applicant.supervisor_user_id if applicant else None
        applicant_job_title = str(applicant.hrm_job_title or "").strip() if applicant else None
    configured_level1_user_id = user_id(LEVEL1_USER_KEY)
    level1_user_id = (
        supervisor_user_id if applicant_user_id else configured_level1_user_id
    )
    # 仅智能人事职务为职员、3级部门负责人或4级部门负责人的员工走一级审批；
    # 其余职务直接进入固定人员二级审批。符合范围但尚未配置审批人的员工仍停留
    # 在一级审批，避免因数据缺失意外绕过审批。
    effective_level1_enabled = (
        level1_enabled
        and requires_level1_approval(applicant)
    )
    return {
        "level_count": level_count,
        "level1_enabled": effective_level1_enabled,
        "level1_configured_enabled": level1_enabled,
        "level1_user_id": level1_user_id,
        "applicant_job_title": applicant_job_title,
        "level2_user_id": user_id(LEVEL2_USER_KEY) if level_count == 2 else None,
        "reminder_interval_minutes": reminder_interval_minutes,
    }


def assign_approval(record, db):
    """Snapshot the policy onto an order or batch when it is submitted."""
    policy = get_approval_policy(db, getattr(record, "created_by", None))
    record.approval_total_levels = policy["level_count"]
    record.approval_level1_required = policy["level1_enabled"]
    record.approval_current_level = 1 if policy["level1_enabled"] else 2
    record.approval_level1_user_id = policy["level1_user_id"]
    record.approval_level2_user_id = policy["level2_user_id"]
    record.approval_stage_started_at = models.china_now()
    record.approval_last_reminded_at = None
    return policy


def refresh_pending_approval(record, db):
    """Apply current settings to an application that is still awaiting approval."""
    policy = get_approval_policy(db, getattr(record, "created_by", None))
    old_level = int(getattr(record, "approval_current_level", None) or 1)
    old_assignee = getattr(record, f"approval_level{old_level}_user_id", None)
    level1_finished = bool(getattr(record, "approval_level1_time", None))
    record.approval_total_levels = policy["level_count"]
    record.approval_level2_user_id = policy["level2_user_id"]
    if level1_finished:
        # Preserve the audit trail, but route the remaining step to the new assignee.
        record.approval_current_level = 2
        if old_level != 2 or old_assignee != policy["level2_user_id"]:
            record.approval_stage_started_at = models.china_now()
            record.approval_last_reminded_at = None
        return policy
    record.approval_level1_required = policy["level1_enabled"]
    record.approval_level1_user_id = policy["level1_user_id"]
    record.approval_current_level = 1 if policy["level1_enabled"] else 2
    new_level = int(record.approval_current_level)
    new_assignee = getattr(record, f"approval_level{new_level}_user_id", None)
    if old_level != new_level or old_assignee != new_assignee:
        record.approval_stage_started_at = models.china_now()
        record.approval_last_reminded_at = None
    return policy


def ensure_approval_snapshot(record, db):
    if not getattr(record, "approval_total_levels", None):
        assign_approval(record, db)


def actor_id_from_request(request):
    current_user = getattr(request.state, "current_user", None)
    if current_user and current_user.get("id"):
        return int(current_user["id"])
    return None


def assert_assigned_approver(record, actor_id, auth_enabled=True):
    """Enforce the snapshotted assignee when authentication is active."""
    level = int(getattr(record, "approval_current_level", None) or 1)
    assigned = getattr(record, f"approval_level{level}_user_id", None)
    if not auth_enabled and actor_id is None:
        return
    if assigned is None:
        raise HTTPException(409, f"第{level}级审批人尚未配置，请先到审批流程配置中设置")
    if actor_id != assigned:
        raise HTTPException(403, f"当前申请应由第{level}级指定审批人处理")


def approve_level(record, actor_id, remark=""):
    """Record one approval. Return True only when the whole flow is complete."""
    level = int(getattr(record, "approval_current_level", None) or 1)
    total = int(getattr(record, "approval_total_levels", None) or 1)
    now = dt.datetime.now()
    setattr(record, f"approval_level{level}_time", now)
    setattr(record, f"approval_level{level}_remark", remark or "")
    if getattr(record, f"approval_level{level}_user_id", None) is None and actor_id:
        setattr(record, f"approval_level{level}_user_id", actor_id)
    if level < total:
        record.approval_current_level = level + 1
        record.approval_stage_started_at = now
        record.approval_last_reminded_at = None
        return False
    record.approver_id = actor_id
    record.approve_time = now
    record.approve_remark = remark or ""
    return True


def reject_level(record, actor_id, remark=""):
    level = int(getattr(record, "approval_current_level", None) or 1)
    now = dt.datetime.now()
    setattr(record, f"approval_level{level}_time", now)
    setattr(record, f"approval_level{level}_remark", remark or "")
    if getattr(record, f"approval_level{level}_user_id", None) is None and actor_id:
        setattr(record, f"approval_level{level}_user_id", actor_id)
    record.approver_id = actor_id
    record.approve_time = now
    record.approve_remark = remark or ""


def save_config(db, key, value, description):
    row = db.query(models.SystemConfig).filter(models.SystemConfig.config_key == key).first()
    if row:
        row.config_value = str(value)
        row.description = description
    else:
        db.add(models.SystemConfig(
            config_key=key, config_value=str(value), description=description
        ))
