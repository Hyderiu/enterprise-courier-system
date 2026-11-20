import io
import unittest

from openpyxl import Workbook

from api.routes.cost import _split_project_path
from config.settings import settings
from utils.excel_importer import parse_projects_excel


class _Upload:
    filename = "projects.xlsx"

    def __init__(self, content):
        self.file = io.BytesIO(content)


class ProjectManagementTests(unittest.TestCase):
    def test_project_path_preserves_backslashes_and_supports_slash_hierarchy(self):
        self.assertEqual(
            _split_project_path(r"AI引擎\自动化平台\26.08\26.12"),
            [r"AI引擎\自动化平台\26.08\26.12"],
        )
        self.assertEqual(
            _split_project_path("市场项目/客户A/视频制作"),
            ["市场项目", "客户A", "视频制作"],
        )

    def test_project_excel_finds_header_after_instructions_and_reads_status(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["项目导入说明", None])
        sheet.append([None, None])
        sheet.append(["项目名称*", "项目状态"])
        sheet.append(["父项目/子项目", "启用"])
        sheet.append(["暂停项目", "停用"])
        output = io.BytesIO()
        workbook.save(output)

        rows, errors = parse_projects_excel(_Upload(output.getvalue()))

        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["project_path"], "父项目/子项目")
        self.assertTrue(rows[0]["enabled"])
        self.assertFalse(rows[1]["enabled"])




class DingTalkFirstLoginTests(unittest.TestCase):
    def setUp(self):
        self.original_login_enabled = settings.dingtalk_login_enabled
        settings.dingtalk_login_enabled = False
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from api.main import app
        from database.connection import Base, get_db

        self.app = app
        self.get_db = get_db
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)

        def override_db():
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        self.app.dependency_overrides[self.get_db] = override_db
        self.client = TestClient(self.app)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        settings.dingtalk_login_enabled = self.original_login_enabled
        self.engine.dispose()

    def test_first_verified_employee_becomes_admin_once_and_is_bound_to_department(self):
        from unittest.mock import Mock, patch
        from database import models

        db = self.session_factory()
        parent = models.Department(
            dept_code="D10", dept_name="运营一部", dingtalk_dept_id="10"
        )
        db.add(parent)
        db.flush()
        db.add(models.Department(
            dept_code="D123", dept_name="运营一组", dingtalk_dept_id="123",
            parent_id=parent.id,
        ))
        db.commit()
        db.close()

        ding_client = Mock()
        ding_client.get_user_by_auth_code.return_value = {
            "userid": "new-user", "unionid": "union-new"
        }
        ding_client.get_user_detail.return_value = {
            "name": "新员工", "avatar": "https://example.test/avatar.png",
            "dept_id_list": [123],
        }
        with patch("dingtalk.DingTalkClient", return_value=ding_client):
            response = self.client.post("/api/auth/dingtalk", json={"auth_code": "code"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["first_login"])
        self.assertEqual(payload["user"]["role"], "admin")
        self.assertEqual(payload["user"]["department"], "运营一部-运营一组")

        db = self.session_factory()
        marker = db.query(models.SystemConfig).filter(
            models.SystemConfig.config_key == "initial_admin_bootstrapped"
        ).first()
        self.assertIsNotNone(marker)
        db.close()

    def test_later_employee_is_not_promoted_after_admin_bootstrap(self):
        from unittest.mock import Mock, patch
        from database import models

        db = self.session_factory()
        db.add(models.User(dingtalk_userid="existing-admin", name="管理员", role="admin"))
        db.add(models.SystemConfig(
            config_key="initial_admin_bootstrapped", config_value="1"
        ))
        db.commit()
        db.close()

        ding_client = Mock()
        ding_client.get_user_by_auth_code.return_value = {"userid": "later-user"}
        ding_client.get_user_detail.return_value = {"name": "后登录员工"}
        with patch("dingtalk.DingTalkClient", return_value=ding_client):
            response = self.client.post("/api/auth/dingtalk", json={"auth_code": "code"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["role"], "user")

    def test_returning_employee_login_does_not_wait_for_directory_profile_calls(self):
        from unittest.mock import Mock, patch
        from database import models

        db = self.session_factory()
        db.add(models.User(
            dingtalk_userid="returning-user", name="现有员工", department="运营中心",
            work_location="厦门", region_code="xiamen_other", role="user", is_active=True,
        ))
        db.commit()
        db.close()

        ding_client = Mock()
        ding_client.get_user_by_auth_code.return_value = {"userid": "returning-user"}
        ding_client.get_user_detail.side_effect = AssertionError("老用户登录不应读取通讯录详情")
        ding_client.list_hrm_employee_fields.side_effect = AssertionError("老用户登录不应同步智能人事")
        with patch("dingtalk.DingTalkClient", return_value=ding_client):
            response = self.client.post("/api/auth/dingtalk", json={"auth_code": "code"})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["first_login"])
        self.assertEqual(response.json()["user"]["department"], "运营中心")
        ding_client.get_user_detail.assert_not_called()
        ding_client.list_hrm_employee_fields.assert_not_called()

    def test_legacy_mock_admin_does_not_block_real_admin_initialization(self):
        from unittest.mock import Mock, patch
        from database import models

        db = self.session_factory()
        db.add(models.User(
            dingtalk_userid="mock_001", name="Mock User", role="admin", is_active=True
        ))
        db.commit()
        db.close()

        ding_client = Mock()
        ding_client.get_user_by_auth_code.return_value = {"userid": "real-admin"}
        ding_client.get_user_detail.return_value = {"name": "真实管理员"}
        with patch("dingtalk.DingTalkClient", return_value=ding_client):
            response = self.client.post("/api/auth/dingtalk", json={"auth_code": "code"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["role"], "admin")
        db = self.session_factory()
        mock_user = db.query(models.User).filter_by(dingtalk_userid="mock_001").first()
        self.assertIsNone(mock_user)
        db.close()

    def test_department_api_returns_complete_hierarchy_name(self):
        from database import models

        db = self.session_factory()
        parent = models.Department(
            dept_code="D20", dept_name="运营二部", dingtalk_dept_id="20"
        )
        db.add(parent)
        db.flush()
        child = models.Department(
            dept_code="D21", dept_name="运营二组", dingtalk_dept_id="21",
            parent_id=parent.id,
        )
        db.add(child)
        db.commit()
        db.close()

        response = self.client.get("/api/departments")
        self.assertEqual(response.status_code, 200)
        items = {item["dept_code"]: item for item in response.json()["items"]}
        self.assertEqual(items["D21"]["dept_name"], "运营二部-运营二组")
        self.assertEqual(items["D21"]["leaf_name"], "运营二组")

if __name__ == "__main__":
    unittest.main()
