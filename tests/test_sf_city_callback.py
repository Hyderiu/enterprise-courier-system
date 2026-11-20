import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.main import app
from config.settings import settings
from database import models
from database.connection import Base, get_db


class SFCityCallbackTests(unittest.TestCase):
    def setUp(self):
        self._login_enabled = settings.dingtalk_login_enabled
        self._callback_token = settings.sf_city_callback_token
        settings.dingtalk_login_enabled = True
        settings.sf_city_callback_token = ""
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine, autocommit=False, autoflush=False
        )

        def override_db():
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)

    def tearDown(self):
        settings.dingtalk_login_enabled = self._login_enabled
        settings.sf_city_callback_token = self._callback_token
        app.dependency_overrides.clear()
        self.engine.dispose()

    def _create_order(self):
        db = self.session_factory()
        try:
            order = models.Order(
                order_no="CITY-LOCAL-1",
                courier_type="SFTC",
                courier_order_no="3368153620852828000",
                sender_name="寄件人",
                sender_phone="13800000000",
                sender_address="北京市测试路1号",
                receiver_name="收件人",
                receiver_phone="13900000000",
                receiver_province="北京市",
                receiver_city="北京市",
                receiver_address="北京市测试路2号",
                item_name="文件",
                status="ordered",
            )
            db.add(order)
            db.commit()
            return order.id
        finally:
            db.close()

    def test_public_health_and_empty_post_return_platform_success_shape(self):
        self.assertEqual(
            self.client.get("/api/courier/sf-city/callback").json(),
            {"error_code": 0, "error_msg": "success"},
        )
        self.assertEqual(
            self.client.post("/api/courier/sf-city/callback").json(),
            {"error_code": 0, "error_msg": "success"},
        )

    def test_delivery_callback_updates_matching_order_and_is_idempotent(self):
        order_id = self._create_order()
        payload = {
            "sf_order_id": 3368153620852828000,
            "shop_order_id": "CITY-LOCAL-1",
            "url_index": "rider_status",
            "order_status": 15,
            "status_desc": "配送员已取货",
            "operator_name": "测试骑士",
            "operator_phone": "13811112222",
            "push_time": 1792728000,
        }
        for _ in range(2):
            response = self.client.post("/api/courier/sf-city/callback", json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["error_code"], 0)

        db = self.session_factory()
        try:
            order = db.query(models.Order).filter(models.Order.id == order_id).one()
            self.assertEqual(order.status, "success")
            self.assertEqual(order.track_status, "配送员已取货")
            self.assertEqual(order.courier_picked_by, "测试骑士")
            self.assertEqual(order.courier_picked_by_phone, "13811112222")
            self.assertEqual(
                db.query(models.CourierTrack).filter(
                    models.CourierTrack.order_id == order_id
                ).count(),
                1,
            )
        finally:
            db.close()

    def test_complete_cancel_and_exception_callbacks_map_to_order_status(self):
        cases = [
            (17, "order_complete", "delivered"),
            (2, "order_cancel", "cancelled"),
            (10, "order_exception", "failed"),
        ]
        for index, (status_code, url_index, expected) in enumerate(cases, start=1):
            db = self.session_factory()
            try:
                order = models.Order(
                    order_no=f"CITY-{index}", courier_type="SFTC",
                    courier_order_no=f"SF-CITY-{index}",
                    sender_name="寄件人", sender_phone="13800000000",
                    sender_address="寄件地址", receiver_name="收件人",
                    receiver_phone="13900000000", receiver_province="北京市",
                    receiver_city="北京市", receiver_address="收件地址",
                    item_name="文件", status="ordered",
                )
                db.add(order)
                db.commit()
                order_id = order.id
            finally:
                db.close()
            response = self.client.post("/api/courier/sf-city/callback", json={
                "sf_order_id": f"SF-CITY-{index}",
                "user_order_id": f"CITY-{index}",
                "order_status": status_code,
                "url_index": url_index,
                "push_time": 1792728000 + index,
            })
            self.assertEqual(response.json()["error_code"], 0)
            db = self.session_factory()
            try:
                self.assertEqual(
                    db.query(models.Order).filter(models.Order.id == order_id).one().status,
                    expected,
                )
            finally:
                db.close()

    def test_callback_queues_assignment_cancel_and_delivery_notifications(self):
        events = [(10, "rider_assigned"), (2, "cancelled"), (17, "delivered")]
        for index, (status_code, expected_event) in enumerate(events, start=1):
            db = self.session_factory()
            try:
                order = models.Order(
                    order_no=f"NOTICE-{index}", courier_type="SF_CITY",
                    courier_order_no=f"SF-NOTICE-{index}", sender_name="寄件人",
                    sender_phone="13800000000", sender_address="寄件地址",
                    receiver_name="收件人", receiver_phone="13900000000",
                    receiver_province="福建省", receiver_city="厦门市",
                    receiver_address="收件地址", item_name="文件", status="ordered",
                )
                db.add(order)
                db.commit()
            finally:
                db.close()
            with patch(
                "api.routes.sf_city_callback.queue_sf_city_status_notification",
                return_value={"queued": True},
            ) as queue:
                response = self.client.post("/api/courier/sf-city/callback", json={
                    "sf_order_id": f"SF-NOTICE-{index}",
                    "shop_order_id": f"NOTICE-{index}",
                    "order_status": status_code,
                    "push_time": 1792729000 + index,
                })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(queue.call_args.args[3], expected_event)

    def test_unmatched_or_single_identifier_is_acknowledged_without_mutation(self):
        order_id = self._create_order()
        response = self.client.post("/api/courier/sf-city/callback", json={
            "shop_order_id": "CITY-LOCAL-1", "order_status": 17
        })
        self.assertEqual(response.json()["error_code"], 0)
        db = self.session_factory()
        try:
            order = db.query(models.Order).filter(models.Order.id == order_id).one()
            self.assertEqual(order.status, "ordered")
            self.assertEqual(db.query(models.CourierTrack).count(), 0)
        finally:
            db.close()

    def test_optional_token_is_enforced_when_configured(self):
        settings.sf_city_callback_token = "CALLBACK-SECRET"
        self.assertEqual(
            self.client.get("/api/courier/sf-city/callback").status_code, 401
        )
        response = self.client.get(
            "/api/courier/sf-city/callback?token=CALLBACK-SECRET"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["error_code"], 0)


if __name__ == "__main__":
    unittest.main()
