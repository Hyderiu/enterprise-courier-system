"""Read projects and departments from CRM and replace local reference data.

The integration intentionally exposes only the six business fields required by
the courier system:

* project code, project name, leaf department code, department full path;
* department code and department full path.

CRM internal integer IDs are used only to join projects to departments and are
never exposed as business codes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from xml.etree import ElementTree
from zipfile import ZipFile

import requests

from config.settings import settings


CRM_OK = 10200
_reference_sync_lock = threading.Lock()
XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
XFT_REFERENCE_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "resources" / "billing_templates" / "project_allocation.xlsx"
)


class CrmAPIError(RuntimeError):
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.data = data
        super().__init__(f"CRM 接口错误 {code}: {message or '未知错误'}")


def _normalize_sign_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    return str(value)


def _auth_headers(app_id: str, secret: str, method: str, params: dict | None) -> dict:
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    values = dict(params or {})
    values["method"] = method.upper()
    normalized = {
        key: _normalize_sign_value(value) for key, value in sorted(values.items())
    }
    payload = json.dumps(
        normalized, separators=(",", ":"), ensure_ascii=False
    )
    message = f"{payload}&timestamp={timestamp}&nonce={nonce}"
    signature = hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    device_id = hashlib.sha256(
        f"courier-system:{app_id}".encode("utf-8")
    ).hexdigest()[:32]
    return {
        "X-CRM-APPID": app_id,
        "X-CRM-TIMESTAMP": timestamp,
        "X-CRM-NONCE": nonce,
        "X-CRM-REQUEST-SIGN": signature,
        "X-CRM-DEVICE-ID": device_id,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _clean_text(value: Any) -> str:
    text = str(value or "").replace("\ufeff", "").strip()
    text = re.sub(r"[\r\n\t]+", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _path_text(parts: Iterable[str]) -> str:
    cleaned = []
    for part in parts:
        value = re.sub(r"[\\/]+", "-", _clean_text(part)).strip(" -")
        if value:
            cleaned.append(value)
    return "-".join(cleaned)


def _reference_key(value: Any) -> str:
    return re.sub(r"\s+", " ", _clean_text(value)).strip().casefold()


def _xlsx_reference_rows(path: Path, sheet_name: str) -> list[list[str]]:
    if not path.is_file():
        return []
    with ZipFile(path) as archive:
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships = ElementTree.fromstring(
            archive.read("xl/_rels/workbook.xml.rels")
        )
        targets = {
            item.get("Id"): item.get("Target")
            for item in relationships.findall(f"{{{PACKAGE_REL_NS}}}Relationship")
        }
        target = None
        for sheet in workbook.iter(f"{{{XLSX_NS}}}sheet"):
            if sheet.get("name") == sheet_name:
                target = targets.get(sheet.get(f"{{{OFFICE_REL_NS}}}id"))
                break
        if not target:
            return []
        entry = target.lstrip("/")
        if not entry.startswith("xl/"):
            entry = f"xl/{entry}"
        root = ElementTree.fromstring(archive.read(entry))
        shared_strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared_strings = [
                "".join(node.text or "" for node in item.iter(f"{{{XLSX_NS}}}t"))
                for item in shared_root.findall(f"{{{XLSX_NS}}}si")
            ]
        rows = []
        for row in root.iter(f"{{{XLSX_NS}}}row"):
            values = {}
            for cell in row.findall(f"{{{XLSX_NS}}}c"):
                column = re.sub(r"\d+$", "", cell.get("r") or "")
                if cell.get("t") == "s":
                    value_node = cell.find(f"{{{XLSX_NS}}}v")
                    value = (
                        shared_strings[int(value_node.text)]
                        if value_node is not None and value_node.text else ""
                    )
                else:
                    value = "".join(
                        node.text or "" for node in cell.iter(f"{{{XLSX_NS}}}t")
                    )
                    if not value:
                        value_node = cell.find(f"{{{XLSX_NS}}}v")
                        value = value_node.text if value_node is not None else ""
                values[column] = value
            rows.append([values.get("A", ""), values.get("B", "")])
        return rows


def _xft_department_map(path: Path = XFT_REFERENCE_TEMPLATE):
    department_rows = _xlsx_reference_rows(path, "部门(附录)")[1:]

    department_candidates = defaultdict(list)
    for code, name in department_rows:
        if _clean_text(code) and _reference_key(name):
            department_candidates[_reference_key(name)].append(_clean_text(code))

    return {
        key: values[0]
        for key, values in department_candidates.items()
        if len(set(values)) == 1
    }


@dataclass(frozen=True)
class DepartmentRow:
    internal_id: int
    raw_dept_id: str
    raw_parent_id: str
    dept_code: str
    leaf_name: str
    full_path: str
    level: int


class CrmReferenceClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        app_id: str | None = None,
        secret: str | None = None,
        timeout: int | None = None,
        retries: int | None = None,
        page_size: int | None = None,
        session: requests.Session | None = None,
        progress_callback: Callable[[dict], None] | None = None,
    ):
        self.base_url = (
            base_url or settings.crm_base_url
        ).rstrip("/")
        self.app_id = (app_id or settings.crm_app_id or "").strip()
        self.secret = (secret or settings.crm_app_secret or "").strip()
        self.timeout = max(5, int(timeout or settings.crm_request_timeout or 30))
        self.retries = max(0, int(
            settings.crm_request_retries if retries is None else retries
        ))
        self.page_size = max(20, min(500, int(
            page_size or settings.crm_page_size or 200
        )))
        self.session = session or requests.Session()
        self.progress_callback = progress_callback
        self._department_reference_cache = None
        if not self.app_id or not self.secret:
            raise ValueError("CRM_APP_ID 或 CRM_APP_SECRET 未配置")

    def _report(self, phase: str, message: str, **stats: Any) -> None:
        if self.progress_callback:
            self.progress_callback({"phase": phase, "message": message, **stats})

    def _department_reference_map(self):
        if self._department_reference_cache is None:
            self._department_reference_cache = _xft_department_map()
        return self._department_reference_cache

    def get(self, path: str, params: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        last_error = None
        for attempt in range(self.retries + 1):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=_auth_headers(self.app_id, self.secret, "GET", params),
                    timeout=self.timeout,
                )
                response.raise_for_status()
                payload = response.json()
                code = int(payload.get("code") or 0)
                if code != CRM_OK:
                    raise CrmAPIError(code, payload.get("msg") or "", payload.get("data"))
                return payload.get("data") if payload.get("data") is not None else []
            except CrmAPIError:
                raise
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"CRM 网络请求失败: {last_error}")

    @staticmethod
    def _flatten_departments(tree: list[dict]) -> list[DepartmentRow]:
        raw_nodes: list[dict] = []

        def collect(nodes: list[dict]) -> None:
            for node in nodes or []:
                if not isinstance(node, dict):
                    continue
                raw_nodes.append(node)
                collect(node.get("children") or [])

        collect(tree)
        by_raw_id = {
            str(node.get("dept_id") or "").strip(): node
            for node in raw_nodes if str(node.get("dept_id") or "").strip()
        }
        path_cache: dict[str, list[str]] = {}
        code_path_cache: dict[str, list[str]] = {}

        def resolve(node: dict, visiting: set[str] | None = None) -> list[str]:
            raw_id = str(node.get("dept_id") or "").strip()
            if raw_id in path_cache:
                return path_cache[raw_id]
            visiting = set(visiting or ())
            if raw_id in visiting:
                return [_clean_text(node.get("name"))]
            visiting.add(raw_id)
            parent = by_raw_id.get(str(node.get("parent_id") or "").strip())
            parts = resolve(parent, visiting) if parent and parent is not node else []
            parts = [*parts, _clean_text(node.get("name"))]
            path_cache[raw_id] = parts
            return parts

        def resolve_code_path(node: dict, visiting: set[str] | None = None) -> list[str]:
            raw_id = str(node.get("dept_id") or "").strip()
            if raw_id in code_path_cache:
                return code_path_cache[raw_id]
            visiting = set(visiting or ())
            if raw_id in visiting:
                return []
            visiting.add(raw_id)
            parent = by_raw_id.get(str(node.get("parent_id") or "").strip())
            parts = resolve_code_path(parent, visiting) if parent and parent is not node else []
            component = _clean_text(node.get("unique_sn") or node.get("dept_id"))
            if component:
                parts = [*parts, component]
            code_path_cache[raw_id] = parts
            return parts

        rows = []
        for node in raw_nodes:
            raw_id = str(node.get("dept_id") or "").strip()
            name = _clean_text(node.get("name"))
            internal_id = int(node.get("id") or 0)
            if not raw_id or not name or not internal_id:
                continue
            parent_id = str(node.get("parent_id") or "").strip()
            is_company_root = parent_id in {"", "0"} or internal_id == 1
            code_parts = resolve_code_path(node)
            if not is_company_root and code_parts[:1] == ["0000"]:
                code_parts = code_parts[1:]
            business_code = "-".join(code_parts) or (
                _clean_text(node.get("unique_sn") or node.get("dept_id"))
                if not is_company_root else "0000"
            )
            parts = resolve(node)
            rows.append(DepartmentRow(
                internal_id=internal_id,
                raw_dept_id=raw_id,
                raw_parent_id=parent_id,
                dept_code=business_code,
                leaf_name=name,
                full_path=_path_text(parts),
                level=max(1, len([part for part in parts if part])),
            ))
        rows.sort(key=lambda item: (item.level, item.full_path, item.dept_code))
        return rows

    def fetch_departments(self) -> list[dict]:
        self._report("departments", "正在读取 CRM 完整部门树")
        data = self.get("/org/department/tree")
        rows = self._flatten_departments(data if isinstance(data, list) else [])
        if not rows:
            raise ValueError("CRM 未返回任何部门，为防止误清空，本次未更新")
        xft_departments = self._department_reference_map()
        rows = [
            DepartmentRow(
                **{
                    **row.__dict__,
                    "dept_code": xft_departments.get(
                        _reference_key(row.full_path), row.dept_code,
                    ),
                }
            )
            for row in rows
        ]
        self._report("departments_done", f"已读取 {len(rows)} 个部门", department_count=len(rows))
        return [row.__dict__.copy() for row in rows]

    def fetch_projects(self, departments: list[dict]) -> list[dict]:
        by_internal_id = {int(row["internal_id"]): row for row in departments}
        by_full_path = {row["full_path"]: row for row in departments}
        self._report("projects", "正在分页读取 CRM 全部项目", page=1)
        page = 1
        total = None
        seen: set[int] = set()
        project_drafts: list[dict] = []
        while True:
            data = self.get("/project/list", params={"page": page, "rows": self.page_size})
            if isinstance(data, dict):
                items = data.get("list") or []
                if data.get("total") is not None:
                    total = int(data.get("total") or 0)
            else:
                items = data if isinstance(data, list) else []
            if not items:
                break
            for item in items:
                project_id = int(item.get("id") or 0)
                unique_sn = _clean_text(item.get("unique_sn"))
                project_name = _clean_text(item.get("full_name") or item.get("name"))
                if not project_id or not unique_sn or not project_name or project_id in seen:
                    continue
                seen.add(project_id)
                charge = item.get("charge_department") or {}
                charge_path = _path_text(
                    str(charge.get("full_name") or charge.get("name") or "").split("/")
                )
                department = (
                    by_internal_id.get(int(charge.get("id") or 0))
                    or by_full_path.get(charge_path)
                )
                department_path = department["full_path"] if department else charge_path
                project_drafts.append({
                    "project_id": project_id,
                    "unique_sn": unique_sn,
                    "project_name": project_name,
                    "dept_code": department["dept_code"] if department else "",
                    "dept_name": department_path,
                })
            self._report(
                "projects", f"正在读取 CRM 项目第 {page} 页",
                page=page, project_count=len(project_drafts), total=total,
            )
            if total is not None:
                if len(seen) >= total:
                    break
            elif len(items) < self.page_size:
                break
            page += 1
            if page > 1000:
                raise RuntimeError("CRM 项目分页超过安全上限")
            time.sleep(0.05)
        if not project_drafts:
            raise ValueError("CRM 未返回任何项目，为防止误清空，本次未更新")
        projects = []
        for item in project_drafts:
            unique_sn = item["unique_sn"]
            project_code = (
                unique_sn if unique_sn.startswith("NB-")
                else f"{unique_sn}-{item['project_id']}"
            )
            projects.append({
                "project_code": project_code,
                "project_name": item["project_name"],
                "dept_code": item["dept_code"],
                "dept_name": item["dept_name"],
            })
        projects.sort(key=lambda item: (item["project_code"], item["project_name"]))
        self._report(
            "projects_done", f"已读取 {len(projects)} 个项目",
            project_count=len(projects), total=total,
        )
        return projects

    def fetch_reference_data(self) -> tuple[list[dict], list[dict]]:
        departments = self.fetch_departments()
        projects = self.fetch_projects(departments)
        return departments, projects


def replace_local_reference_data_from_crm(db, client: CrmReferenceClient | None = None) -> dict:
    """Atomically replace local project/department reference tables from CRM."""
    if not _reference_sync_lock.acquire(blocking=False):
        raise RuntimeError("CRM 项目和部门同步已在进行中")
    try:
        from database import models

        client = client or CrmReferenceClient()
        departments, projects = client.fetch_reference_data()
        department_codes = [row["dept_code"] for row in departments]
        project_codes = [row["project_code"] for row in projects]
        if len(department_codes) != len(set(department_codes)):
            raise ValueError("CRM 返回了重复部门编码，本次未更新")
        if len(project_codes) != len(set(project_codes)):
            raise ValueError("CRM 返回了重复项目编号，本次未更新")

        db.query(models.Project).delete(synchronize_session=False)
        db.query(models.Department).update(
            {models.Department.parent_id: None}, synchronize_session=False
        )
        db.query(models.Department).delete(synchronize_session=False)
        db.flush()

        local_by_raw_id = {}
        for row in departments:
            parent = local_by_raw_id.get(row["raw_parent_id"])
            department = models.Department(
                dept_code=row["dept_code"],
                dept_name=row["leaf_name"],
                dingtalk_dept_id=row["raw_dept_id"],
                dingtalk_parent_id=row["raw_parent_id"],
                parent_id=parent.id if parent else None,
                level=row["level"],
                enabled=True,
                is_synced_from_dingtalk=False,
            )
            db.add(department)
            db.flush()
            local_by_raw_id[row["raw_dept_id"]] = department

        for row in projects:
            db.add(models.Project(
                project_code=row["project_code"],
                project_name=row["project_name"],
                # full_path remains a unique technical key. UI/API always expose
                # project_name and the separate department full path.
                full_path=row["project_code"],
                parent_id=None,
                level=1,
                dept_code=row["dept_code"] or None,
                dept_name=row["dept_name"] or None,
                enabled=True,
            ))
        db.commit()
        missing_department = sum(1 for row in projects if not row["dept_code"])
        return {
            "department_count": len(departments),
            "project_count": len(projects),
            "projects_without_department": missing_department,
            "current_count": len(projects),
            "source": "CRM",
        }
    except Exception:
        db.rollback()
        raise
    finally:
        _reference_sync_lock.release()
