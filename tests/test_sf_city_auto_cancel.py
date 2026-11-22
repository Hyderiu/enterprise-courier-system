import datetime as dt
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import models
from database.connection import Base
from scheduler.jobs import cancel_stale_sf_city_orders


class SFCityAutoCancelTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_unassigned_order_is_rechecked_then_cancelled_at_deadline(self):
        db = self.factory()
        order = models.Order(
            order_no="CITY-AUTO-CANCEL", courier_type="SF_CITY",
            courier_order_no="SF-CITY-AUTO-CANCEL", status="ordered",
            track_status="寻找骑手中", order_time=dt.datetime(2026, 11, 9, 9, 0),
            sender_name="寄件人", sender_phone="13800000009", sender_address="取件地址",
            receiver_name="收件人", receiver_phone="13900000000",
            receiver_province="福建省", receiver_city="厦门市",
            receiver_address="收件地址", item_name="文件",
        )
        db.add(order)
        db.commit()
        order_id = order.id
        db.close()

        courier = Mock()
        courier.cancel_order.return_value = {
            "success": True, "message": "顺丰同城订单已取消",
        }
        with patch(
            "database.connection.SessionLocal", self.factory,
        ), patch(
            "api.routes.orders._refresh_order_tracking",
            return_value={"previous_status": "ordered"},
        ), patch(
            "courier.get_courier_for_order", return_value=courier,
        ), patch(
            "utils.approval_notifications.send_sf_city_status_notification_once",
            return_value={"sent": True},
        ) as notice, patch(
            "scheduler.jobs.dt", wraps=dt,
        ), patch(
            "config.settings.settings.sf_city_rider_wait_minutes", 30,
        ):
            result = cancel_stale_sf_city_orders(
                now=dt.datetime(2026, 11, 9, 9, 31)
            )

        self.assertEqual(result["cancelled"], 1)
        courier.cancel_order.assert_called_once_with(
            "SF-CITY-AUTO-CANCEL", "CITY-AUTO-CANCEL"
        )
        notice.assert_called_once()
        verify = self.factory()
        try:
            saved = verify.query(models.Order).filter_by(id=order_id).one()
            self.assertEqual(saved.status, "cancelled")
            self.assertIn("30分钟无人接单", saved.track_status)
            self.assertEqual(verify.query(models.CourierTrack).count(), 1)
        finally:
            verify.close()


if __name__ == "__main__":
    unittest.main()
