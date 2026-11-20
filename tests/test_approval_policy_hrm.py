import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import models
from database.connection import Base
from api.main import _apply_global_approval_settings, _apply_supervisor_approval_settings
from utils.approval_policy import (
    LEVEL1_ENABLED_KEY, LEVEL2_USER_KEY, LEVEL_COUNT_KEY,
    REMINDER_INTERVAL_MINUTES_KEY, get_approval_policy,
    refresh_pending_approval,
)


class SmartHrmApprovalPolicyTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(engine)
        self.engine = engine
        self.db = sessionmaker(bind=engine)()
        self.supervisor = models.User(name="主管", dingtalk_userid="manager")
        self.level2 = models.User(name="二级审批人", dingtalk_userid="level2")
        self.db.add_all([self.supervisor, self.level2])
        self.db.flush()
        self.db.add_all([
            models.SystemConfig(config_key=LEVEL_COUNT_KEY, config_value="2"),
            models.SystemConfig(config_key=LEVEL1_ENABLED_KEY, config_value="1"),
            models.SystemConfig(config_key=LEVEL2_USER_KEY, config_value=str(self.level2.id)),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _policy(self, title, with_supervisor=True, override=None):
        user = models.User(
            name=title, dingtalk_userid=f"u-{title}", hrm_job_title=title,
            supervisor_user_id=self.supervisor.id if with_supervisor else None,
            approval_level1_required_override=override,
        )
        self.db.add(user)
        self.db.commit()
        return get_approval_policy(self.db, user.id)

    def test_only_requested_hrm_titles_use_level_one(self):
        for title in ("职员", "3级部门负责人", "4级部门负责人"):
            with self.subTest(title=title):
                policy = self._policy(title)
                self.assertTrue(policy["level1_enabled"])
                self.assertEqual(policy["level1_user_id"], self.supervisor.id)

    def test_other_titles_go_directly_to_level_two(self):
        for title in ("1级部门负责人", "2级部门负责人", "2+级部门负责人", "CEO", "分管副总裁"):
            with self.subTest(title=title):
                policy = self._policy(title)
                self.assertFalse(policy["level1_enabled"])
                self.assertEqual(policy["level2_user_id"], self.level2.id)

    def test_eligible_employee_without_approver_cannot_bypass_level_one(self):
        policy = self._policy("职员", with_supervisor=False)
        self.assertTrue(policy["level1_enabled"])
        self.assertIsNone(policy["level1_user_id"])

    def test_admin_can_skip_level_one_for_otherwise_eligible_employee(self):
        self.assertFalse(self._policy("职员", override=False)["level1_enabled"])

    def test_admin_can_require_level_one_for_otherwise_ineligible_employee(self):
        policy = self._policy("2级部门负责人", override=True)
        self.assertTrue(policy["level1_enabled"])
        self.assertEqual(policy["level1_user_id"], self.supervisor.id)

    def test_pending_application_is_rerouted_when_settings_change(self):
        applicant = models.User(
            name="申请人", dingtalk_userid="applicant", hrm_job_title="职员",
            supervisor_user_id=self.supervisor.id,
        )
        self.db.add(applicant)
        self.db.flush()
        order = models.Order(
            order_no="CO-PENDING", courier_type="SF", status="pending",
            sender_name="寄件人", sender_phone="13800000000", sender_address="寄件地址",
            receiver_name="收件人", receiver_phone="13900000000",
            receiver_province="广东省", receiver_city="深圳市", receiver_district="南山区",
            receiver_address="收件地址", item_name="文件",
            created_by=applicant.id, approval_total_levels=2,
            approval_current_level=2, approval_level1_required=False,
            approval_level2_user_id=self.supervisor.id,
        )
        self.db.add(order)
        self.db.commit()

        refresh_pending_approval(order, self.db)

        self.assertEqual(order.approval_current_level, 1)
        self.assertTrue(order.approval_level1_required)
        self.assertEqual(order.approval_level1_user_id, self.supervisor.id)
        self.assertEqual(order.approval_level2_user_id, self.level2.id)

    def test_two_settings_sections_save_independently(self):
        applicant = models.User(
            name="申请人", dingtalk_userid="applicant-split", hrm_job_title="职员",
            supervisor_user_id=self.supervisor.id,
            approval_level1_required_override=True,
        )
        self.db.add(applicant)
        self.db.commit()

        _apply_global_approval_settings({
            "level1_enabled": False, "level2_user_id": self.level2.id,
        }, self.db)
        self.assertEqual(applicant.supervisor_user_id, self.supervisor.id)
        self.assertTrue(applicant.approval_level1_required_override)

        original_level2 = get_approval_policy(self.db)["level2_user_id"]
        payload = {str(user.id): 0 for user in self.db.query(models.User).all()}
        payload[str(applicant.id)] = self.level2.id
        _apply_supervisor_approval_settings({"supervisors": payload}, self.db)
        self.assertEqual(applicant.supervisor_user_id, self.level2.id)
        self.assertTrue(applicant.approval_level1_required_override)
        self.assertEqual(get_approval_policy(self.db)["level2_user_id"], original_level2)

    def test_global_save_persists_reminder_interval_with_approvers(self):
        _apply_global_approval_settings({
            "level1_enabled": True,
            "level2_user_id": self.level2.id,
            "reminder_interval_minutes": 120,
        }, self.db)
        self.db.commit()

        policy = get_approval_policy(self.db)
        stored = self.db.query(models.SystemConfig).filter(
            models.SystemConfig.config_key == REMINDER_INTERVAL_MINUTES_KEY
        ).one()
        self.assertEqual(policy["reminder_interval_minutes"], 120)
        self.assertEqual(stored.config_value, "120")


if __name__ == "__main__":
    unittest.main()
