import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.routes.approval import ApproveReq, skip_level
from api.routes.batch import skip_batch_level
from database.connection import Base
from database import models


class AdminApprovalSkipTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.db = self.factory()
        self.admin = models.User(
            dingtalk_userid="skip-admin", name="管理员甲", role="admin", is_active=True,
        )
        self.employee = models.User(
            dingtalk_userid="skip-employee", name="普通员工", role="employee", is_active=True,
        )
        self.db.add_all([self.admin, self.employee])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def request(self, user, role, *, auth_enabled=True, headers=None):
        return SimpleNamespace(headers=headers or {}, state=SimpleNamespace(
            current_user={"id": user.id, "name": user.name, "role": role},
            auth_enabled=auth_enabled,
        ))

    @staticmethod
    def order(**values):
        defaults = dict(
            order_no="SKIP-ORDER", courier_type="SF", status="pending",
            sender_name="寄件人", sender_phone="13800000000", sender_address="寄件地址",
            receiver_name="收件人", receiver_phone="13900000000",
            receiver_province="福建省", receiver_city="厦门市",
            receiver_address="收件地址", item_name="文件",
            approval_total_levels=2, approval_current_level=1,
            approval_level1_required=True,
        )
        defaults.update(values)
        return models.Order(**defaults)

    def test_employee_cannot_skip_an_approval_level(self):
        order = self.order()
        self.db.add(order)
        self.db.commit()
        with self.assertRaises(HTTPException) as caught:
            skip_level(
                order.id, 1, ApproveReq(), self.request(self.employee, "employee"),
                BackgroundTasks(), self.db,
            )
        self.assertEqual(caught.exception.status_code, 403)

    @patch("api.routes.approval.queue_approval_notification", return_value={"queued": True})
    @patch("api.routes.approval.queue_approval_progress_notification", return_value={"queued": True})
    def test_preview_admin_view_can_exercise_skip_button(self, *_mocks):
        order = self.order(approval_level1_user_id=self.employee.id)
        self.db.add(order)
        self.db.commit()
        preview_request = SimpleNamespace(
            headers={"X-Preview-Role": "admin"},
            state=SimpleNamespace(current_user=None, auth_enabled=False),
        )
        result = skip_level(
            order.id, 1, ApproveReq(), preview_request, BackgroundTasks(), self.db,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["current_level"], 2)

    @patch("api.routes.approval.queue_approval_notification", return_value={"queued": True})
    @patch("api.routes.approval.queue_approval_progress_notification", return_value={"queued": True})
    def test_admin_skip_level_one_is_audited_and_moves_to_level_two(self, *_mocks):
        order = self.order(approval_level1_user_id=self.employee.id)
        self.db.add(order)
        self.db.commit()
        result = skip_level(
            order.id, 1, ApproveReq(remark="主管出差"), self.request(self.admin, "admin"),
            BackgroundTasks(), self.db,
        )
        self.db.refresh(order)
        self.assertTrue(result["ok"])
        self.assertFalse(result["approval_completed"])
        self.assertEqual(order.approval_current_level, 2)
        self.assertEqual(order.approval_level1_user_id, self.admin.id)
        self.assertIn("[管理员跳过]", order.approval_level1_remark)
        self.assertIn("主管出差", order.approval_level1_remark)

    @patch("api.routes.approval.queue_dispatch_notification", return_value={"queued": True})
    @patch("api.routes.approval.queue_approval_result_notification", return_value={"queued": True})
    @patch("api.routes.approval.dispatch_order", return_value={"ok": True, "message": "下单成功"})
    def test_admin_skip_level_two_completes_and_dispatches(self, dispatch, *_mocks):
        order = self.order(
            approval_current_level=2, approval_level1_time=models.china_now(),
            approval_level2_user_id=self.employee.id,
        )
        self.db.add(order)
        self.db.commit()
        result = skip_level(
            order.id, 2, ApproveReq(), self.request(self.admin, "admin"),
            BackgroundTasks(), self.db,
        )
        self.db.refresh(order)
        self.assertTrue(result["approval_completed"])
        self.assertTrue(result["dispatch_ok"])
        self.assertEqual(order.approval_level2_user_id, self.admin.id)
        self.assertIn("[管理员跳过]", order.approval_level2_remark)
        dispatch.assert_called_once()

    @patch("api.routes.batch.queue_approval_notification", return_value={"queued": True})
    @patch("api.routes.batch.queue_approval_progress_notification", return_value={"queued": True})
    def test_batch_skip_updates_batch_and_every_child(self, *_mocks):
        batch = models.Batch(
            batch_no="SKIP-BATCH", courier_type="SF", sender_name="寄件人",
            sender_phone="13800000000", item_name="文件", status="pending_approval",
            approval_total_levels=2, approval_current_level=1,
            approval_level1_required=True, approval_level1_user_id=self.employee.id,
        )
        self.db.add(batch)
        self.db.flush()
        child = self.order(order_no="SKIP-BATCH-ORDER", batch_id=batch.id,
                           approval_level1_user_id=self.employee.id)
        self.db.add(child)
        self.db.commit()
        result = skip_batch_level(
            batch.id, 1, self.request(self.admin, "admin"), BackgroundTasks(),
            {"remark": "紧急寄件"}, self.db,
        )
        self.db.refresh(batch)
        self.db.refresh(child)
        self.assertFalse(result["approval_completed"])
        self.assertEqual(batch.approval_current_level, 2)
        self.assertEqual(child.approval_current_level, 2)
        self.assertEqual(batch.approval_level1_user_id, self.admin.id)
        self.assertEqual(child.approval_level1_user_id, self.admin.id)
        self.assertIn("紧急寄件", batch.approval_level1_remark)


if __name__ == "__main__":
    unittest.main()
