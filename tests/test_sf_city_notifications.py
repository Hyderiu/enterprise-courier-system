import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.order_dispatch import dispatch_order, parse_delivery
from database.connection import Base
from database import models
from utils.approval_notifications import (
    build_sf_city_status_notification,
    queue_sf_city_status_notification,
    sf_city_event_from_transition,
)


class SFCityLifecycleNotificationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = models.User(
            name="申请员工", dingtalk_userid="employee-1", phone="13800000009"
        )
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _order(self, **values):
        defaults = dict(
            order_no="CO-CITY-1", courier_type="SF_CITY", region_code="xiamen_other",
            status="ordered", created_by=self.user.id,
            sender_name="申请员工", sender_phone="13800000009",
            sender_address="取件地址", receiver_name="收件人",
            receiver_phone="13900000000", receiver_province="福建省",
            receiver_city="厦门市", receiver_address="收件地址",
            item_name="文件", item_weight=1,
        )
        defaults.update(values)
        order = models.Order(**defaults)
        self.db.add(order)
        self.db.commit()
        self.db.refresh(order)
        return order

    def test_searching_notice_is_queued_once_and_persisted(self):
        order = self._order(courier_order_no="SF-CITY-1")
        background = BackgroundTasks()
        with patch("utils.approval_notifications.settings.public_base_url", "https://express.example"):
            first = queue_sf_city_status_notification(
                background, self.db, order, "searching"
            )
            second = queue_sf_city_status_notification(
                background, self.db, order, "searching"
            )
        self.assertEqual(first, {"queued": True})
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(background.tasks), 1)
        self.assertEqual(json.loads(order.sf_city_notification_events), ["searching"])

    def test_searching_notice_explains_timeout_tip_and_direct_detail_action(self):
        order = self._order(courier_order_no="SF-CITY-NOTICE")
        with patch(
            "utils.approval_notifications.settings.public_base_url",
            "https://express.example",
        ), patch(
            "utils.approval_notifications.settings.sf_city_rider_wait_minutes", 30,
        ):
            notice, _ = build_sf_city_status_notification(
                self.db, order, "searching"
            )
        self.assertIn("30 分钟内仍无骑士接单", notice["markdown"])
        self.assertIn("增加小费", notice["markdown"])
        self.assertEqual(notice["action_title"], "查看订单详情 / 加小费")
        self.assertIn("dingtalk-notification-entry", notice["action_url"])

    def test_lifecycle_messages_match_business_stages(self):
        order = self._order(
            courier_order_no="SF-CITY-2", courier_picked_by="骑士甲",
            courier_picked_by_phone="13811112222",
        )
        with patch("utils.approval_notifications.settings.public_base_url", "https://express.example"):
            assigned, _ = build_sf_city_status_notification(
                self.db, order, "rider_assigned"
            )
            delivered, _ = build_sf_city_status_notification(
                self.db, order, "delivered"
            )
        self.assertIn("已找到骑士", assigned["markdown"])
        self.assertIn("骑士甲", assigned["markdown"])
        self.assertIn("13811112222", assigned["markdown"])
        self.assertEqual(assigned["action_title"], "查看订单详情")
        self.assertIn("已送达", delivered["markdown"])
        self.assertEqual(delivered["action_title"], "查看订单详情")

    def test_cancelled_notice_only_claims_no_rider_before_assignment(self):
        waiting = self._order(order_no="CO-CITY-WAITING")
        assigned = self._order(
            order_no="CO-CITY-ASSIGNED", courier_picked_by="骑士甲"
        )
        with patch("utils.approval_notifications.settings.public_base_url", "https://express.example"):
            waiting_notice, _ = build_sf_city_status_notification(
                self.db, waiting, "cancelled", "超时未接单"
            )
            assigned_notice, _ = build_sf_city_status_notification(
                self.db, assigned, "cancelled", "用户取消"
            )
        self.assertIn("未找到骑士，订单已取消", waiting_notice["markdown"])
        self.assertIn("顺丰同城订单已取消", assigned_notice["markdown"])
        self.assertNotIn("未找到骑士", assigned_notice["markdown"])

    def test_polling_transition_detects_assignment_cancel_and_delivery(self):
        order = SimpleNamespace(
            courier_type="SF_CITY", status="ordered", track_status="骑士已接单",
            courier_picked_by="骑士甲",
        )
        self.assertEqual(
            sf_city_event_from_transition(order, "ordered", "", "寻找骑手中"),
            "rider_assigned",
        )
        order.status = "cancelled"
        self.assertEqual(
            sf_city_event_from_transition(order, "ordered", "", "寻找骑手中"),
            "cancelled",
        )
        order.status = "delivered"
        self.assertEqual(
            sf_city_event_from_transition(order, "success", "骑士甲", "配送中"),
            "delivered",
        )

    def test_waiting_placeholder_does_not_trigger_rider_assignment(self):
        order = SimpleNamespace(
            courier_type="SF_CITY", status="ordered", track_status="寻找骑手中",
            courier_picked_by="暂未有骑士接单",
        )
        self.assertIsNone(sf_city_event_from_transition(
            order, "ordered", "", "寻找骑手中"
        ))

    def test_unix_delivery_timestamp_uses_china_time(self):
        parsed = parse_delivery(1794214828)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, None)

    def test_successful_city_dispatch_starts_in_searching_state(self):
        order = self._order(status="approved")
        courier = Mock()
        courier.create_order.return_value = {
            "success": True, "courier_order_no": "SF-CITY-3",
            "courier_order_nos": ["SF-CITY-3"], "raw_response": {"error_code": 0},
            "estimated_delivery": 1794214828,
        }
        with patch("courier.get_courier_for_order", return_value=courier):
            result = dispatch_order(order, self.db)
        self.assertTrue(result["ok"])
        self.assertEqual(order.status, "ordered")
        self.assertIsNotNone(order.estimated_delivery_time)
        self.assertEqual(order.track_status, "寻找骑手中")
        track = self.db.query(models.CourierTrack).filter_by(order_id=order.id).one()
        self.assertEqual(track.track_status, "寻找骑手中")
        self.assertIn("正在查找骑士", track.track_detail)


if __name__ == "__main__":
    unittest.main()
