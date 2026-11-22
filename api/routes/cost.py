from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi import BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional
import hashlib
import io
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from database.connection import get_db, SessionLocal
from database import models
from config.settings import settings
from crm import CrmReferenceClient, replace_local_reference_data_from_crm
from utils.department_paths import build_department_path_map

router = APIRouter()

_supervisor_sync_lock = threading.Lock()
_supervisor_sync_state = {
    "status": "idle", "total": 0, "read": 0, "failed": 0,
    "unresolved": 0, "message": "", "stats": None,
}
_crm_sync_state_lock = threading.Lock()
_crm_sync_state = {"status": "idle", "message": "", "stats": None}
_crm_sync_thread = None
_department_sync_state_lock = threading.Lock()
_department_sync_state = {
    "status": "idle", "phase": "", "message": "", "stats": None,
    "started_at": None, "finished_at": None,
}


# ========== 部门 ==========

@router.get("/departments")
def list_departments(enabled_only: bool = True, db: Session = Depends(get_db)):
    q = db.query(models.Department)
    if enabled_only:
        q = q.filter(models.Department.enabled == True)
    departments = q.order_by(models.Department.dept_code).all()
    paths = build_department_path_map(
        db.query(models.Department).all()
    )
    return {"ok": True, "items": [
        {
            "id": d.id, "dept_code": d.dept_code,
            "dept_name": paths.get(d.id, d.dept_name),
            "leaf_name": d.dept_name, "full_name": paths.get(d.id, d.dept_name),
            "enabled": d.enabled,
        }
        for d in departments
    ]}


class DeptUpsert(BaseModel): dept_code: str; dept_name: str; enabled: bool = True


@router.post("/admin/departments")
def upsert_department(body: DeptUpsert, db: Session = Depends(get_db)):
    d = db.query(models.Department).filter(models.Department.dept_code == body.dept_code).first()
    if d:
        d.dept_name = body.dept_name; d.enabled = body.enabled
    else:
        d = models.Department(dept_code=body.dept_code, dept_name=body.dept_name, enabled=body.enabled)
        db.add(d)
    db.commit(); db.refresh(d)
    return {"ok": True, "id": d.id, "dept_code": d.dept_code, "dept_name": d.dept_name}


@router.post("/admin/departments/bulk")
async def bulk_import_departments(file: UploadFile = File(...), db: Session = Depends(get_db)):
    from utils.excel_importer import parse_departments_excel
    if not (file.filename or "").lower().endswith((".xlsx", ".xls", ".csv")):
        raise HTTPException(400, "只支持 .xlsx、.xls 或 .csv 文件")
    try:
        rows, errors = parse_departments_excel(file)
    except ValueError as exc:
        status = 413 if "不能超过" in str(exc) else 400
        raise HTTPException(status, str(exc))
    added, updated = 0, 0
    for r in rows:
        d = db.query(models.Department).filter(models.Department.dept_code == r["dept_code"]).first()
        if d:
            d.dept_name = r["dept_name"]; updated += 1
        else:
            db.add(models.Department(dept_code=r["dept_code"], dept_name=r["dept_name"])); added += 1
    db.commit()
    return {"ok": True, "added": added, "updated": updated, "errors": errors}


@router.delete("/admin/departments/{dept_code}")
def delete_department(dept_code: str, db: Session = Depends(get_db)):
    d = db.query(models.Department).filter(models.Department.dept_code == dept_code).first()
    if not d: raise HTTPException(404, "Not found")
    db.delete(d); db.commit()
    return {"ok": True}


# ========== 项目 ==========

def _project_dict(p):
    display_name = re.sub(r"[\\/]+", "-", p.project_name or "")
    # CRM 项目是扁平业务对象；full_path 在库内仅作为唯一技术键。
    # 下单页和管理页必须完整展示项目名称，不能把技术键当名称。
    display_path = display_name
    return {
        "id": p.id, "project_code": p.project_code, "project_name": display_name,
        "full_path": display_path, "parent_id": p.parent_id,
        "level": p.level or 1, "dept_code": p.dept_code, "dept_name": p.dept_name,
        "enabled": bool(p.enabled),
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _generated_project_code(full_path: str) -> str:
    return "PRJ-" + hashlib.sha1(full_path.encode("utf-8")).hexdigest()[:12].upper()


def _split_project_path(raw: str) -> List[str]:
    parts = [part.strip() for part in re.split(r"[\/]+|\s+-\s+", raw or "") if part.strip()]
    if not parts:
        raise ValueError("项目名称为空")
    if len(parts) > 5:
        raise ValueError("项目层级最多支持 5 级")
    return parts


def _descendant_ids(db: Session, root_ids: List[int]) -> set:
    result = set(root_ids)
    frontier = set(root_ids)
    while frontier:
        children = db.query(models.Project.id).filter(
            models.Project.parent_id.in_(frontier)
        ).all()
        frontier = {row[0] for row in children if row[0] not in result}
        result.update(frontier)
    return result


def _enable_ancestors(db: Session, project: models.Project):
    current = project
    while current:
        current.enabled = True
        current = db.query(models.Project).filter(
            models.Project.id == current.parent_id
        ).first() if current.parent_id else None


def _refresh_descendant_paths(db: Session, project: models.Project):
    children = db.query(models.Project).filter(models.Project.parent_id == project.id).all()
    for child in children:
        child.level = (project.level or 1) + 1
        child.full_path = f"{project.full_path}/{child.project_name}"
        _refresh_descendant_paths(db, child)


def _upsert_project_path(db: Session, raw_path: str, *, enabled: bool = True,
                         project_code: Optional[str] = None,
                         dept_code: Optional[str] = None,
                         dept_name: Optional[str] = None):
    parts = _split_project_path(raw_path)
    parent = None
    current_path = ""
    for index, part in enumerate(parts):
        current_path = part if not current_path else f"{current_path}/{part}"
        project = db.query(models.Project).filter(models.Project.full_path == current_path).first()
        if not project:
            code = project_code if index == len(parts) - 1 and project_code else _generated_project_code(current_path)
            project = models.Project(
                project_code=code, project_name=part, full_path=current_path,
                parent_id=parent.id if parent else None, level=index + 1,
                enabled=True if index < len(parts) - 1 else enabled,
                dept_code=dept_code if index == len(parts) - 1 else None,
                dept_name=dept_name if index == len(parts) - 1 else None,
            )
            db.add(project)
            db.flush()
        elif index == len(parts) - 1:
            project.enabled = enabled
            project.dept_code = dept_code
            project.dept_name = dept_name
        if enabled:
            _enable_ancestors(db, project)
        parent = project
    return parent


def _project_recency_key(project):
    """Sort CRM project codes with an embedded YYYYMMDD date newest first."""
    code = str(project.project_code or "")
    match = re.search(r"(?<!\d)((?:19|20)\d{6})(?!\d)", code)
    return (match.group(1) if match else "", code)


@router.get("/projects")
def list_projects(
    dept_code: Optional[str] = None,
    sort: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(models.Project).filter(models.Project.enabled == True)
    if dept_code:
        q = q.filter(models.Project.dept_code == dept_code)
    projects = q.order_by(models.Project.full_path, models.Project.project_name).all()
    if sort == "recent":
        projects.sort(key=_project_recency_key, reverse=True)
    return {"ok": True, "items": [_project_dict(p) for p in projects]}


class ProjectUpsert(BaseModel):
    project_code: Optional[str] = None
    project_name: str
    parent_id: Optional[int] = None
    dept_code: Optional[str] = None
    dept_name: Optional[str] = None
    enabled: bool = True


class ProjectBulkAction(BaseModel):
    ids: List[int]
    action: str


def _set_crm_sync_state(**values):
    with _crm_sync_state_lock:
        values["updated_at"] = time.time()
        _crm_sync_state.update(values)


def _crm_sync_snapshot():
    with _crm_sync_state_lock:
        state = dict(_crm_sync_state)
    thread = _crm_sync_thread
    state["worker_alive"] = bool(thread and thread.is_alive())
    return state


def _run_crm_reference_sync():
    db = None
    try:
        _set_crm_sync_state(
            status="running", message="CRM 项目和部门同步线程已启动",
            stats={"phase": "thread_started"},
        )
        db = SessionLocal()

        def report_progress(progress):
            _set_crm_sync_state(
                status="running",
                message=progress.get("message") or "正在只读同步 CRM 项目和部门",
                stats=progress,
            )

        client = CrmReferenceClient(progress_callback=report_progress)
        stats = replace_local_reference_data_from_crm(db, client=client)
        _set_crm_sync_state(
            status="done", message="CRM 项目和部门同步完成", stats=stats,
            finished_at=time.time(),
        )
    except BaseException as exc:
        if db is not None:
            db.rollback()
        _set_crm_sync_state(
            status="failed", message=str(exc),
            stats={"phase": "worker_failed", "error_type": type(exc).__name__},
            finished_at=time.time(),
        )
    finally:
        if db is not None:
            db.close()


@router.post("/admin/reference/sync-crm")
def sync_reference_from_crm():
    """后台只读刷新 CRM 项目和部门，避免页面请求超时。"""
    state = _crm_sync_snapshot()
    if state["status"] == "running":
        return {"ok": True, "started": False, "state": state, "readonly": True}
    if not settings.crm_app_id or not settings.crm_app_secret:
        raise HTTPException(500, "CRM_APP_ID 或 CRM_APP_SECRET 未配置")
    global _crm_sync_thread
    _set_crm_sync_state(
        status="running", message="正在启动 CRM 只读同步", stats=None,
        started_at=time.time(),
    )
    _crm_sync_thread = threading.Thread(
        target=_run_crm_reference_sync, name="crm-reference-sync", daemon=True,
    )
    _crm_sync_thread.start()
    return {"ok": True, "started": True, "state": _crm_sync_snapshot(), "readonly": True}


@router.get("/admin/reference/sync-crm/status")
def crm_reference_sync_status():
    return {"ok": True, "state": _crm_sync_snapshot(), "readonly": True}


@router.post("/admin/projects")
def create_project(body: ProjectUpsert, db: Session = Depends(get_db)):
    name = body.project_name.strip()
    if not name:
        raise HTTPException(400, "项目名称不能为空")
    parent = None
    if body.parent_id:
        parent = db.query(models.Project).filter(models.Project.id == body.parent_id).first()
        if not parent:
            raise HTTPException(404, "父项目不存在")
    full_path = f"{parent.full_path}/{name}" if parent else name
    if db.query(models.Project).filter(models.Project.full_path == full_path).first():
        raise HTTPException(409, "同一层级下已存在同名项目")
    p = models.Project(
        project_code=body.project_code or _generated_project_code(full_path),
        project_name=name, full_path=full_path,
        parent_id=parent.id if parent else None,
        level=(parent.level or 1) + 1 if parent else 1,
        dept_code=body.dept_code, dept_name=body.dept_name,
        enabled=body.enabled,
    )
    db.add(p)
    db.flush()
    if p.enabled:
        _enable_ancestors(db, p)
    db.commit()
    db.refresh(p)
    return {"ok": True, "item": _project_dict(p)}


@router.put("/admin/projects/{pid}")
def update_project(pid: int, body: ProjectUpsert, db: Session = Depends(get_db)):
    p = db.query(models.Project).filter(models.Project.id == pid).first()
    if not p:
        raise HTTPException(404, "项目不存在")
    name = body.project_name.strip()
    if not name:
        raise HTTPException(400, "项目名称不能为空")
    parent = db.query(models.Project).filter(models.Project.id == p.parent_id).first() if p.parent_id else None
    full_path = f"{parent.full_path}/{name}" if parent else name
    duplicate = db.query(models.Project).filter(
        models.Project.full_path == full_path, models.Project.id != pid
    ).first()
    if duplicate:
        raise HTTPException(409, "同一层级下已存在同名项目")
    p.project_name = name
    p.full_path = full_path
    p.dept_code = body.dept_code
    p.dept_name = body.dept_name
    p.enabled = body.enabled
    if body.project_code:
        p.project_code = body.project_code
    if p.enabled:
        _enable_ancestors(db, p)
    else:
        for child_id in _descendant_ids(db, [p.id]):
            child = db.query(models.Project).filter(models.Project.id == child_id).first()
            if child:
                child.enabled = False
    _refresh_descendant_paths(db, p)
    db.commit()
    db.refresh(p)
    return {"ok": True, "item": _project_dict(p)}


@router.post("/admin/projects/bulk")
async def bulk_import_projects(file: UploadFile = File(...), db: Session = Depends(get_db)):
    from utils.excel_importer import parse_projects_excel
    if not (file.filename or "").lower().endswith((".xlsx", ".xls", ".csv")):
        raise HTTPException(400, "只支持 .xlsx、.xls 或 .csv 文件")
    try:
        rows, errors = parse_projects_excel(file)
    except ValueError as exc:
        status = 413 if "不能超过" in str(exc) else 400
        raise HTTPException(status, str(exc))
    added = updated = 0
    before_paths = {row[0] for row in db.query(models.Project.full_path).all() if row[0]}
    for index, row in enumerate(rows, start=1):
        try:
            leaf = _upsert_project_path(
                db, row["project_path"], enabled=row.get("enabled", True),
                project_code=row.get("project_code") or None,
                dept_code=row.get("dept_code") or None,
                dept_name=row.get("dept_name") or None,
            )
            if leaf.full_path in before_paths:
                updated += 1
            else:
                added += 1
                before_paths.add(leaf.full_path)
        except Exception as exc:
            errors.append(f"数据第{index}条: {exc}")
    db.commit()
    return {"ok": True, "added": added, "updated": updated, "errors": errors,
            "total": added + updated, "project_nodes": db.query(models.Project).count()}


@router.post("/admin/projects/bulk-action")
def bulk_project_action(body: ProjectBulkAction, db: Session = Depends(get_db)):
    ids = sorted(set(body.ids))
    if not ids:
        raise HTTPException(400, "请先选择项目")
    projects = db.query(models.Project).filter(models.Project.id.in_(ids)).all()
    if not projects:
        raise HTTPException(404, "未找到所选项目")
    if body.action == "enable":
        for project in projects:
            _enable_ancestors(db, project)
    elif body.action == "disable":
        affected = _descendant_ids(db, ids)
        db.query(models.Project).filter(models.Project.id.in_(affected)).update(
            {models.Project.enabled: False}, synchronize_session=False
        )
    elif body.action == "delete":
        affected = _descendant_ids(db, ids)
        db.query(models.Project).filter(models.Project.id.in_(affected)).delete(
            synchronize_session=False
        )
    else:
        raise HTTPException(400, "不支持的操作")
    db.commit()
    return {"ok": True, "affected": len(projects) if body.action == "enable" else len(affected)}


@router.delete("/admin/projects/{pid}")
def delete_project(pid: int, db: Session = Depends(get_db)):
    if not db.query(models.Project).filter(models.Project.id == pid).first():
        raise HTTPException(404, "项目不存在")
    affected = _descendant_ids(db, [pid])
    db.query(models.Project).filter(models.Project.id.in_(affected)).delete(
        synchronize_session=False
    )
    db.commit()
    return {"ok": True, "affected": len(affected)}


@router.get("/admin/projects/export")
def export_projects(db: Session = Depends(get_db)):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "项目列表"
    ws.append(["项目编号", "项目名称", "执行部门编码", "执行部门完整路径"])
    for p in db.query(models.Project).order_by(models.Project.project_code).all():
        ws.append([p.project_code, p.project_name, p.dept_code or "", p.dept_name or ""])
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 70
    ws.column_dimensions["C"].width = 22
    ws.column_dimensions["D"].width = 85
    buf = io.BytesIO()
    wb.save(buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=projects_export.xlsx"},
    )


# ========== 模板下载 ==========

from fastapi.responses import Response

@router.get("/admin/template/{file_type}")
def download_template(file_type: str):
    from utils.excel_importer import generate_template
    data = generate_template(file_type)
    filename = "projects_template.xlsx" if file_type == "projects" else "departments_template.xlsx"
    return Response(
        content=data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# ============ 钉钉部门同步 ============
def _set_department_sync_state(**values):
    with _department_sync_state_lock:
        _department_sync_state.update(values)


def _department_sync_snapshot():
    with _department_sync_state_lock:
        return dict(_department_sync_state)


def _run_department_sync():
    db = SessionLocal()
    try:
        # 部门树已由 CRM 统一维护。钉钉在这里仅刷新员工资料，绝不覆盖部门表。
        from dingtalk import DingTalkClient
        client = DingTalkClient()
        _set_department_sync_state(
            status="running", phase="users", message="正在读取钉钉员工资料",
        )
        user_stats = _sync_users_dept(db, client)
        changed = user_stats.get("created", 0) + user_stats.get("updated", 0)
        result = {
            "message": (
                f"钉钉员工同步完成，共 {user_stats.get('total', 0)} 人，"
                f"本次变化 {changed} 人"
            ),
            "stats": {"users_synced": user_stats},
        }
        _set_department_sync_state(
            status="done", phase="done", message=result["message"],
            stats=result["stats"], finished_at=time.time(),
        )
    except Exception as exc:
        db.rollback()
        message = exc.detail if isinstance(exc, HTTPException) else str(exc)
        _set_department_sync_state(
            status="failed", phase="failed", message=message,
            stats=None, finished_at=time.time(),
        )
    finally:
        db.close()


@router.post("/admin/users/sync-dingtalk")
def sync_users_from_dingtalk():
    """只同步钉钉员工资料；项目和部门始终由 CRM 维护。"""
    state = _department_sync_snapshot()
    if state["status"] == "running":
        return {"ok": True, "started": False, "state": state}
    _set_department_sync_state(
        status="running", phase="starting", message="正在启动钉钉员工同步",
        stats=None, started_at=time.time(), finished_at=None,
    )
    threading.Thread(target=_run_department_sync, daemon=True).start()
    return {"ok": True, "started": True, "state": _department_sync_snapshot()}


@router.get("/admin/users/sync-dingtalk/status")
def dingtalk_user_sync_status():
    return {"ok": True, "state": _department_sync_snapshot()}


def _sync_users_dept(db, client):
    """读取通讯录快照，只写入新增或字段确有变化的员工。"""
    created_userids = set()
    updated_userids = set()
    manager_userids = {}
    # CRM 的 dept_id 与钉钉通讯录 dept_id 一致，可直接关联员工；部门名称
    # 和层级仍以 CRM 为唯一来源。
    depts = db.query(models.Department).filter(
        models.Department.dingtalk_dept_id.isnot(None),
        models.Department.enabled.is_(True),
    ).all()
    paths = build_department_path_map(db.query(models.Department).all())
    dept_by_dingtalk_id = {
        str(department.dingtalk_dept_id): department for department in depts
        if department.dingtalk_dept_id is not None
    }
    # 根名称直接取 CRM 部门表，避免每次员工同步额外调用
    # /v2/department/get。部门树的唯一数据源仍然是 CRM。
    root_department = db.query(models.Department).filter(
        models.Department.parent_id.is_(None),
        models.Department.enabled.is_(True),
    ).order_by(models.Department.level.asc(), models.Department.id.asc()).first()
    root_name = str((root_department.dept_name if root_department else None) or "公司").strip()
    all_users_loader = getattr(client, "list_all_users", None)
    if callable(all_users_loader):
        users = all_users_loader(dept_by_dingtalk_id)
    else:
        # Compatibility for test doubles and older adapters.
        users = client.list_users_by_dept(1, recursive=True)

    for user_data in users:
        uid = str(user_data.get("userid") or "").strip()
        if not uid:
            continue
        manager_uid = str(user_data.get("manager_userid") or "").strip()
        if manager_uid:
            manager_userids[uid] = manager_uid
        memberships = []
        for dept_id in user_data.get("dept_id_list", []) or []:
            department = dept_by_dingtalk_id.get(str(dept_id))
            if department:
                memberships.append(department)
        selected_department = max(memberships, key=lambda item: item.level or 1, default=None)
        department_name = (
            paths.get(selected_department.id, selected_department.dept_name)
            if selected_department else root_name
        )
        existing = db.query(models.User).filter(
            models.User.dingtalk_userid == uid
        ).first()
        if not existing:
            db.add(models.User(
                dingtalk_userid=uid, dingtalk_unionid=user_data.get("unionid"),
                name=user_data.get("name"), phone=user_data.get("mobile"),
                avatar=user_data.get("avatar"), department=department_name,
                role="user",
            ))
            created_userids.add(uid)
            continue

        changed = False
        for field, value in (
            ("dingtalk_unionid", user_data.get("unionid")),
            ("name", user_data.get("name")),
            ("phone", user_data.get("mobile")),
            ("avatar", user_data.get("avatar")),
            ("department", department_name),
        ):
            if value and getattr(existing, field) != value:
                setattr(existing, field, value)
                changed = True
        if changed:
            updated_userids.add(uid)
    db.flush()
    local_users = {
        str(user.dingtalk_userid): user for user in db.query(models.User).filter(
            models.User.dingtalk_userid.isnot(None)
        ).all()
    }
    managers_linked = 0
    for employee_uid, manager_uid in manager_userids.items():
        employee = local_users.get(employee_uid)
        manager = local_users.get(manager_uid)
        if not employee:
            continue
        if employee.approval_level1_required_override is not None:
            # The administrator explicitly saved this employee's approval route.
            # Directory refreshes must not silently undo that decision.
            continue
        manager_id = manager.id if manager and manager.id != employee.id else None
        if employee.supervisor_user_id != manager_id:
            employee.supervisor_user_id = manager_id
            if employee_uid not in created_userids:
                updated_userids.add(employee_uid)
        if manager_id:
            managers_linked += 1
    db.commit()
    changed_count = len(created_userids) + len(updated_userids)
    return {
        "total": len(users),
        "created": len(created_userids),
        "updated": len(updated_userids),
        "unchanged": max(0, len(users) - changed_count),
        "managers_linked": managers_linked,
    }


def _sync_all_supervisors(db, client, progress=None, preserve_manual=True):
    """Read Smart HR job title + manager and persist only approval-related fields."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    users = db.query(models.User).filter(
        models.User.dingtalk_userid.isnot(None), models.User.is_active.is_(True)
    ).all()
    by_dingtalk_id = {str(user.dingtalk_userid): user for user in users}
    # Acquire one token and resolve the real Smart HR field instead of relying on
    # a hard-coded custom-field UUID.
    client._access_token()
    job_field_code = client.find_hrm_field_code("职务")
    if not job_field_code:
        raise RuntimeError("智能人事花名册中没有找到“职务”字段")
    userids = list(by_dingtalk_id)
    chunks = [userids[index:index + 20] for index in range(0, len(userids), 20)]

    def fetch(chunk):
        last_error = None
        for attempt in range(3):
            try:
                rows = client.list_hrm_employee_fields(chunk)
                parsed = []
                for row in rows:
                    fields = {
                        str(field.get("field_code") or ""): field
                        for field in row.get("field_list", []) or []
                    }
                    manager_field = fields.get("sys00-reportManagerId") or {}
                    job_field = fields.get(job_field_code) or {}
                    parsed.append({
                        "userid": str(row.get("userid") or "").strip(),
                        "manager_userid": str(
                            manager_field.get("value") or manager_field.get("label") or ""
                        ).strip(),
                        "job_title": str(
                            job_field.get("label") or job_field.get("value") or ""
                        ).strip(),
                        "work_location": str(
                            (fields.get("sys00-workPlace") or {}).get("label")
                            or (fields.get("sys00-workPlace") or {}).get("value") or ""
                        ).strip(),
                    })
                return parsed
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.4 * (attempt + 1))
        raise last_error

    results = {}
    errors = []
    completed = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch, chunk): chunk for chunk in chunks}
        for future in as_completed(futures):
            chunk = futures[future]
            try:
                for item in future.result():
                    if item["userid"]:
                        results[item["userid"]] = item
            except Exception as exc:
                errors.append({"count": len(chunk), "error": str(exc)})
            completed += len(chunk)
            if progress:
                progress(
                    total=len(users), read=min(completed, len(users)),
                    failed=sum(error["count"] for error in errors),
                )

    linked = cleared = unchanged = unresolved = job_updated = 0
    for employee_uid, item in results.items():
        user = by_dingtalk_id.get(employee_uid)
        if not user:
            continue
        manager_uid = item["manager_userid"]
        manager = by_dingtalk_id.get(manager_uid) if manager_uid else None
        if preserve_manual and user.approval_level1_required_override is not None:
            unchanged += 1
        elif manager_uid and not manager:
            unresolved += 1
        elif manager_uid:
            new_manager_id = manager.id if manager and manager.id != user.id else None
            if user.supervisor_user_id == new_manager_id:
                unchanged += 1
            else:
                user.supervisor_user_id = new_manager_id
                if new_manager_id:
                    linked += 1
                else:
                    cleared += 1
        else:
            # Smart HR 中未填写主管时保留管理员手工配置，避免定时同步把
            # 已补齐的一级审批人重新清空。
            unchanged += 1
        job_title = item["job_title"] or None
        if user.hrm_job_title != job_title:
            user.hrm_job_title = job_title
            job_updated += 1
        work_location = item.get("work_location") or None
        if work_location:
            from utils.regions import region_from_work_location
            user.work_location = work_location
            user.region_code = region_from_work_location(work_location)
    db.commit()
    return {
        "total": len(users), "read": len(results), "linked": linked,
        "cleared": cleared, "unchanged": unchanged,
        "unresolved": unresolved,
        "failed": sum(error["count"] for error in errors),
        "job_updated": job_updated,
        "job_field_code": job_field_code,
        "errors": errors[:10],
    }


def _set_supervisor_sync_state(**values):
    with _supervisor_sync_lock:
        _supervisor_sync_state.update(values)


def _supervisor_sync_snapshot():
    with _supervisor_sync_lock:
        return dict(_supervisor_sync_state)


def _run_supervisor_sync(preserve_manual=True):
    from dingtalk.client import DingTalkClient

    db = SessionLocal()
    try:
        total = db.query(models.User).filter(
            models.User.dingtalk_userid.isnot(None), models.User.is_active.is_(True)
        ).count()
        _set_supervisor_sync_state(
            status="running", total=total, read=0, failed=0, unresolved=0,
            message="正在读取钉钉主管关系", stats=None,
        )

        def progress(**values):
            _set_supervisor_sync_state(**values)

        stats = _sync_all_supervisors(
            db, DingTalkClient(), progress=progress,
            preserve_manual=preserve_manual,
        )
        if stats["failed"] and stats["read"] == 0:
            message = stats["errors"][0]["error"] if stats["errors"] else "全部员工读取失败"
            raise RuntimeError(message)
        _set_supervisor_sync_state(
            status="done", total=stats["total"], read=stats["read"],
            failed=stats["failed"], unresolved=stats["unresolved"],
            message="主管关系同步完成", stats=stats,
        )
    except Exception as exc:
        db.rollback()
        _set_supervisor_sync_state(status="failed", message=str(exc), stats=None)
    finally:
        db.close()


@router.post("/admin/supervisors/sync-dingtalk")
def sync_all_supervisors_from_dingtalk(background_tasks: BackgroundTasks):
    """Explicit manual full Smart HR refresh; never called by the daily job."""
    state = _supervisor_sync_snapshot()
    if state["status"] == "running":
        return {"ok": True, "started": False, "state": state}
    _set_supervisor_sync_state(
        status="running", total=0, read=0, failed=0, unresolved=0,
        message="正在准备同步", stats=None,
    )
    # Clicking this explicit button intentionally refreshes the manually
    # configured relationship. Automatic jobs never run the paid full HR sync.
    background_tasks.add_task(_run_supervisor_sync, False)
    return {"ok": True, "started": True, "state": _supervisor_sync_snapshot()}


@router.get("/admin/supervisors/sync-dingtalk/status")
def supervisor_sync_status():
    return {"ok": True, "state": _supervisor_sync_snapshot()}


@router.get("/departments/tree")
def get_department_tree(db: Session = Depends(get_db)):
    """返回树形结构的部门列表，前端渲染费用归属选择器用。"""
    rows = db.query(models.Department).filter(models.Department.enabled == True)            .order_by(models.Department.level, models.Department.dingtalk_sort).all()
    paths = build_department_path_map(rows)
    nodes = [{"id": r.id, "dept_code": r.dept_code, "dept_name": r.dept_name,
              "full_name": paths.get(r.id, r.dept_name),
              "level": r.level, "parent_id": r.parent_id,
              "from_dingtalk": r.is_synced_from_dingtalk or False} for r in rows]
    # 组装树
    by_id = {n["id"]: {**n, "children": []} for n in nodes}
    roots = []
    for nid, n in by_id.items():
        if n["parent_id"] and n["parent_id"] in by_id:
            by_id[n["parent_id"]]["children"].append(n)
        else:
            roots.append(n)
    return {"ok": True, "tree": roots, "flat": nodes}
