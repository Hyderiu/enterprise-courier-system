import unittest
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.routes.admin_users import RoleUpdate, _assert_not_last_admin, update_user_role
from api.routes.auth import _refresh_login_profile
from api.routes.cost import _sync_users_dept
from database import models
from database.connection import Base


class _Session(dict):
    pass


class AdminUserTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_directory_sync_creates_unseen_people_and_preserves_existing_role(self):
        department = models.Department(
            dept_code="D10", dept_name="研发部", dingtalk_dept_id="10",
            level=1, is_synced_from_dingtalk=True,
        )
        self.db.add(department)
        self.db.add(models.User(
            dingtalk_userid="u1", name="旧姓名", role="user", department="旧部门",
            approval_level1_required_override=True,
        ))
        self.db.commit()

        class Client:
            def get_root_department(self):
                raise AssertionError("employee sync must not call department/get")

            def list_users_by_dept(self, dept_id, recursive):
                self.args = (dept_id, recursive)
                return [
                    {"userid": "u1", "name": "审核员", "mobile": "13800000001", "dept_id_list": [10], "manager_userid": "u2"},
                    {"userid": "u2", "name": "未登录员工", "mobile": "13800000002", "dept_id_list": [10]},
                ]

        client = Client()
        stats = _sync_users_dept(self.db, client)
        self.assertEqual(client.args, (1, True))
        self.assertEqual(stats, {
            "total": 2, "created": 1, "updated": 1, "unchanged": 0,
            "managers_linked": 0,
        })
        existing = self.db.query(models.User).filter_by(dingtalk_userid="u1").one()
        unseen = self.db.query(models.User).filter_by(dingtalk_userid="u2").one()
        self.assertEqual(existing.role, "user")
        self.assertEqual(existing.department, "研发部")
        self.assertIsNone(existing.supervisor_user_id)
        self.assertEqual(unseen.role, "user")

    def test_login_refresh_reads_only_current_employee_profile(self):
        user = models.User(
            dingtalk_userid="u1", name="员工", work_location="旧地点",
            region_code="xiamen_other",
        )

        class Client:
            def list_hrm_employee_fields(self, userids):
                self.userids = userids
                return [{
                    "userid": "u1",
                    "field_list": [
                        {"field_code": "sys00-workPlace", "label": "成都办公室"},
                        {"field_code": "sys00-reportManagerId", "value": "manager-1"},
                        {"field_code": "custom-title", "field_name": "职务", "label": "职员"},
                    ],
                }]

        client = Client()
        manager_userid = _refresh_login_profile(client, user)
        self.assertEqual(client.userids, ["u1"])
        self.assertEqual(manager_userid, "manager-1")
        self.assertEqual(user.work_location, "成都办公室")
        self.assertEqual(user.region_code, "chengdu")
        self.assertEqual(user.hrm_job_title, "职员")

    def test_last_admin_cannot_be_demoted(self):
        admin = models.User(dingtalk_userid="admin", name="管理员", role="admin", is_active=True)
        self.db.add(admin)
        self.db.commit()
        with self.assertRaises(HTTPException) as error:
            _assert_not_last_admin(self.db, admin, next_role="user")
        self.assertEqual(error.exception.status_code, 400)

    def test_second_admin_can_be_demoted_and_session_role_is_updated(self):
        first = models.User(dingtalk_userid="a1", name="管理员一", role="admin", is_active=True)
        second = models.User(dingtalk_userid="a2", name="管理员二", role="admin", is_active=True)
        self.db.add_all([first, second])
        self.db.commit()
        request = SimpleNamespace(
            session=_Session(user_id=second.id, role="admin"),
            state=SimpleNamespace(current_user={"id": second.id, "role": "admin"}),
        )
        result = update_user_role(second.id, RoleUpdate(role="user"), request, self.db)
        self.assertEqual(result["user"]["role"], "user")
        self.assertEqual(request.session["role"], "employee")


if __name__ == "__main__":
    unittest.main()
