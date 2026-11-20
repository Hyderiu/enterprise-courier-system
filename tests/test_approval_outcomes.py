import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.main import app
from config.settings import settings
from database.connection import Base, get_db
from database.models import Order


class ApprovalOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.original_login = settings.dingtalk_login_enabled
        settings.dingtalk_login_enabled = False
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)

        def override_db():
            db = self.factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        settings.dingtalk_login_enabled = self.original_login
        self.engine.dispose()

    def test_approval_success_is_not_reported_as_failure_when_dispatch_fails(self):
        db = self.factory()
        order = Order(
            order_no="CO-APPROVAL-DISPATCH-FAIL", courier_type="JDL",
            region_code="chengdu", express_type="ed-m-0001",
            express_type_name="京东标快", status="pending",
            approval_total_levels=1, approval_current_level=1,
            sender_name="寄件人", sender_phone="13800000000", sender_address="测试路1号",
            receiver_name="收件人", receiver_phone="13900000000",
            receiver_province="湖南省", receiver_city="长沙市",
            receiver_address="测试路2号", item_name="文件", item_weight=1,
        )
        db.add(order)
        db.commit()
        order_id = order.id
        db.close()

        dispatch_result = {
            "ok": False, "status": "failed", "error_code": "JD-403",
            "message": "月结账号无下单权限", "courier_order_no": None,
        }
        with patch("api.routes.approval.dispatch_order", return_value=dispatch_result), patch(
            "api.routes.approval.queue_approval_result_notification", return_value={"queued": True}
        ):
            response = self.client.post(f"/api/approval/{order_id}/approve", json={})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["approval_completed"])
        self.assertFalse(data["dispatch_ok"])
        self.assertIn("审批已通过", data["message"])
        self.assertIn("月结账号无下单权限", data["message"])
        self.assertIn("JD-403", data["message"])

    def test_sf_city_final_approval_reports_searching_for_rider(self):
        db = self.factory()
        order = Order(
            order_no="CO-CITY-APPROVAL", courier_type="SF_CITY",
            region_code="xiamen_other", express_type="10",
            express_type_name="快件", status="pending",
            approval_total_levels=1, approval_current_level=1,
            sender_name="申请人", sender_phone="13800000009",
            sender_address="店铺地址", receiver_name="收件人",
            receiver_phone="13900000000", receiver_province="福建省",
            receiver_city="厦门市", receiver_address="收件地址",
            item_name="文件", item_weight=1,
        )
        db.add(order)
        db.commit()
        order_id = order.id
        db.close()
        dispatch_result = {
            "ok": True, "status": "ordered", "courier_order_no": "SF-CITY-1",
            "message": "下单成功",
        }
        with patch("api.routes.approval.dispatch_order", return_value=dispatch_result), patch(
            "api.routes.approval.queue_approval_result_notification"
        ) as generic_notice, patch(
            "api.routes.approval.queue_sf_city_status_notification",
            return_value={"queued": True},
        ) as city_notice, patch(
            "api.routes.approval.queue_dispatch_notification",
            return_value={"queued": True},
        ):
            response = self.client.post(f"/api/approval/{order_id}/approve", json={})
        self.assertEqual(response.status_code, 200)
        self.assertIn("正在查找骑士", response.json()["message"])
        generic_notice.assert_not_called()
        self.assertEqual(city_notice.call_args.args[3], "searching")


if __name__ == "__main__":
    unittest.main()
