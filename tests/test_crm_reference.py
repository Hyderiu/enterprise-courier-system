import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.routes.cost import list_departments, list_projects
from crm.reference import (
    CrmReferenceClient, _reference_key, _xft_department_map,
    replace_local_reference_data_from_crm,
)
from database import models
from database.connection import Base


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, *, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers, timeout))
        if url.endswith("/org/department/tree"):
            return FakeResponse({"code": 10200, "data": DEPARTMENT_TREE})
        page = int((params or {}).get("page", 1))
        return FakeResponse({"code": 10200, "data": self.pages.get(page, {"list": [], "total": 0})})


DEPARTMENT_TREE = [{
    "id": 1,
    "dept_id": "1",
    "parent_id": "0",
    "unique_sn": "0000",
    "name": "示例科技有限公司",
    "children": [{
        "id": 10,
        "dept_id": "917876266",
        "parent_id": "1",
        "unique_sn": "100139",
        "name": "行政管理部",
        "children": [{
            "id": 11,
            "dept_id": "10001",
            "parent_id": "917876266",
            "unique_sn": "101001",
            "name": "行政一组",
            "children": [],
        }],
    }],
}]


class CrmReferenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_departments_use_business_code_and_complete_dash_path(self):
        rows = CrmReferenceClient._flatten_departments(DEPARTMENT_TREE)
        mapped = {row.dept_code: row for row in rows}
        self.assertEqual(mapped["0000"].full_path, "示例科技有限公司")
        self.assertEqual(
            mapped["100139"].full_path,
            "示例科技有限公司-行政管理部",
        )
        self.assertEqual(
            mapped["100139-101001"].full_path,
            "示例科技有限公司-行政管理部-行政一组",
        )

    def test_missing_unique_sn_falls_back_to_raw_department_id_in_full_code_path(self):
        tree = [{
            "id": 1, "dept_id": "1", "parent_id": "0", "unique_sn": "0000",
            "name": "公司", "children": [{
                "id": 2, "dept_id": "900", "parent_id": "1", "unique_sn": "100002",
                "name": "运营中心", "children": [{
                    "id": 3, "dept_id": "608622659", "parent_id": "900",
                    "unique_sn": "", "name": "运营六部", "children": [],
                }],
            }],
        }]
        mapped = {row.dept_code: row for row in CrmReferenceClient._flatten_departments(tree)}
        self.assertIn("100002-608622659", mapped)

    def test_project_pagination_uses_total_when_server_caps_page_size(self):
        pages = {
            1: {"list": [{
                "id": 6186, "unique_sn": "20261021-003", "name": "项目一",
                "charge_department": {"id": 11},
            }], "total": 2},
            2: {"list": [{
                "id": 7000, "unique_sn": "NB-20261020-002", "full_name": "测试二",
                "charge_department": {"id": 10},
            }], "total": 2},
        }
        session = FakeSession(pages)
        client = CrmReferenceClient(
            app_id="app", secret="secret", page_size=200, session=session
        )
        client._department_reference_cache = {}
        departments = client.fetch_departments()
        projects = client.fetch_projects(departments)
        mapped = {item["project_code"]: item for item in projects}
        self.assertIn("20261021-003-6186", mapped)
        self.assertIn("NB-20261020-002", mapped)
        self.assertEqual(mapped["20261021-003-6186"]["dept_code"], "100139-101001")
        self.assertEqual(
            mapped["20261021-003-6186"]["dept_name"],
            "示例科技有限公司-行政管理部-行政一组",
        )
        project_calls = [call for call in session.calls if call[0].endswith("/project/list")]
        self.assertEqual(len(project_calls), 2)

    def test_public_template_contains_no_private_department_reference(self):
        self.assertEqual(_xft_department_map(), {})

    def test_all_projects_use_new_project_code_rule_without_xft_filtering(self):
        pages = {1: {"list": [{
            "id": 6186, "unique_sn": "20261021-003", "name": "项目一",
            "charge_department": {"id": 11},
        }, {
            "id": 5000, "unique_sn": "20250101-001", "name": "老项目",
            "charge_department": {"id": 11},
        }], "total": 2}}
        client = CrmReferenceClient(
            app_id="app", secret="secret", page_size=200, session=FakeSession(pages)
        )
        client._department_reference_cache = {}
        departments = client.fetch_departments()
        projects = client.fetch_projects(departments)
        self.assertEqual([project["project_code"] for project in projects], [
            "20250101-001-5000",
            "20261021-003-6186",
        ])

    def test_archived_internal_department_id_can_match_current_exact_path(self):
        pages = {1: {"list": [{
            "id": 6186,
            "unique_sn": "20261021-003",
            "name": "历史项目",
            "charge_department": {
                "id": 999999,
                "full_name": "示例科技有限公司/行政管理部/行政一组",
            },
        }], "total": 1}}
        client = CrmReferenceClient(
            app_id="app", secret="secret", page_size=200, session=FakeSession(pages)
        )
        client._department_reference_cache = {}
        departments = client.fetch_departments()
        project = client.fetch_projects(departments)[0]
        self.assertEqual(project["dept_code"], "100139-101001")
        self.assertEqual(
            project["dept_name"],
            "示例科技有限公司-行政管理部-行政一组",
        )

    def test_atomic_replace_and_order_page_apis_expose_only_business_values(self):
        class Client:
            def fetch_reference_data(self):
                departments = [row.__dict__.copy() for row in CrmReferenceClient._flatten_departments(DEPARTMENT_TREE)]
                projects = [{
                    "project_code": "20261021-003-6186",
                    "project_name": "完整项目名称",
                    "dept_code": "100139-101001",
                    "dept_name": "示例科技有限公司-行政管理部-行政一组",
                }]
                return departments, projects

        result = replace_local_reference_data_from_crm(self.db, client=Client())
        self.assertEqual(result["department_count"], 3)
        self.assertEqual(result["project_count"], 1)
        departments = list_departments(db=self.db)["items"]
        projects = list_projects(db=self.db)["items"]
        self.assertEqual(projects[0]["project_name"], "完整项目名称")
        self.assertEqual(projects[0]["full_path"], "完整项目名称")
        self.assertEqual(projects[0]["dept_code"], "100139-101001")
        paths = {item["dept_code"]: item["dept_name"] for item in departments}
        self.assertEqual(
            paths["100139-101001"], "示例科技有限公司-行政管理部-行政一组"
        )

    def test_order_page_projects_can_be_sorted_from_recent_to_old(self):
        self.db.add_all([
            models.Project(
                project_code="20261021-003", project_name="较新项目",
                full_path="较新项目", enabled=True,
            ),
            models.Project(
                project_code="NB-20261020-002", project_name="较旧项目",
                full_path="较旧项目", enabled=True,
            ),
            models.Project(
                project_code="LEGACY", project_name="无日期项目",
                full_path="无日期项目", enabled=True,
            ),
        ])
        self.db.commit()

        items = list_projects(sort="recent", db=self.db)["items"]

        self.assertEqual(
            [item["project_code"] for item in items],
            ["20261021-003", "NB-20261020-002", "LEGACY"],
        )
        template = Path("web/templates/index.html").read_text(encoding="utf-8")
        self.assertIn("/api/projects?sort=recent", template)

    def test_management_page_has_exact_crm_columns_and_wrap_protection(self):
        template = Path("web/templates/admin_cost.html").read_text(encoding="utf-8")
        self.assertIn("<th>部门编码</th><th>部门完整路径</th>", template)
        self.assertIn(
            "<th>项目编号</th><th>项目名称</th><th>执行部门编码</th><th>执行部门完整路径</th>",
            template,
        )
        self.assertIn("overflow-wrap:anywhere", template)
        self.assertIn("height:clamp(280px,36vh,380px);overflow-y:auto", template)
        self.assertIn('id="department-search"', template)
        self.assertIn('id="project-search"', template)
        self.assertIn("CRM 未配置", template)
        self.assertNotIn("仅展示部门编码", template)
        self.assertNotIn("仅同步项目编号", template)
        self.assertNotIn("飞书", template)
        self.assertNotIn("钉钉部门", template)


if __name__ == "__main__":
    unittest.main()
