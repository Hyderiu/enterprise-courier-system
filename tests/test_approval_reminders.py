import datetime as dt
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import models
from database.connection import Base
from database.schema_upgrade import upgrade_schema
from scheduler.jobs import send_pending_approval_reminders
from utils.approval_policy import (
    LEVEL2_USER_KEY, LEVEL_COUNT_KEY, REMINDER_INTERVAL_MINUTES_KEY,
)


class PendingApprovalReminderTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        db = self.Session()
        self.applicant = models.User(name="申请人", dingtalk_userid="applicant")
        self.approver = models.User(name="二级审批人", dingtalk_userid="approver")
        db.add_all([self.applicant, self.approver])
        db.flush()
        self.applicant_id = self.applicant.id
        self.approver_id = self.approver.id
        db.add_all([
            models.SystemConfig(config_key=LEVEL_COUNT_KEY, config_value="2"),
            models.SystemConfig(config_key=LEVEL2_USER_KEY, config_value=str(self.approver_id)),
            models.SystemConfig(
                config_key=REMINDER_INTERVAL_MINUTES_KEY, config_value="60"
            ),
        ])
        db.commit()
        db.close()

    def tearDown(self):
        self.engine.dispose()

    def _add_order(self, started_at):
        db = self.Session()
        order = models.Order(
            order_no="REMINDER-1", courier_type="SF", status="pending",
            sender_name="寄件人", sender_phone="13800000000", sender_address="寄件地址",
            receiver_name="收件人", receiver_phone="13900000000",
            receiver_province="福建省", receiver_city="厦门市",
            receiver_address="收件地址", item_name="文件",
            created_by=self.applicant_id, approval_total_levels=2,
            approval_current_level=2, approval_level1_required=False,
            approval_level2_user_id=self.approver_id,
            approval_stage_started_at=started_at,
        )
        db.add(order)
        db.commit()
        order_id = order.id
        db.close()
        return order_id

    def test_repeats_every_interval_and_stops_after_approval(self):
        now = dt.datetime(2026, 11, 6, 10, 0, 0)
        order_id = self._add_order(now - dt.timedelta(minutes=61))
        with patch("database.connection.SessionLocal", self.Session), patch(
            "utils.approval_notifications.settings.public_base_url", "https://express.example"
        ), patch(
            "utils.approval_notifications.send_approval_notification",
            return_value={"sent": True},
        ) as sender:
            first = send_pending_approval_reminders(now=now)
            too_soon = send_pending_approval_reminders(now=now + dt.timedelta(minutes=59))
            second = send_pending_approval_reminders(now=now + dt.timedelta(minutes=60))
            db = self.Session()
            order = db.query(models.Order).filter(models.Order.id == order_id).one()
            order.status = "approved"
            db.commit()
            db.close()
            stopped = send_pending_approval_reminders(now=now + dt.timedelta(minutes=120))

        self.assertEqual(first["sent"], 1)
        self.assertEqual(too_soon["sent"], 0)
        self.assertEqual(second["sent"], 1)
        self.assertEqual(stopped["checked"], 0)
        self.assertEqual(sender.call_count, 2)
        self.assertIn("仍在等待您审批", sender.call_args.args[0]["markdown"])

    def test_changed_interval_takes_effect_on_next_scheduler_scan(self):
        now = dt.datetime(2026, 11, 6, 10, 0, 0)
        self._add_order(now - dt.timedelta(minutes=61))
        db = self.Session()
        interval = db.query(models.SystemConfig).filter(
            models.SystemConfig.config_key == REMINDER_INTERVAL_MINUTES_KEY
        ).one()
        interval.config_value = "120"
        db.commit()
        db.close()

        with patch("database.connection.SessionLocal", self.Session), patch(
            "utils.approval_notifications.settings.public_base_url", "https://express.example"
        ), patch(
            "utils.approval_notifications.send_approval_notification",
            return_value={"sent": True},
        ) as sender:
            before_due = send_pending_approval_reminders(now=now)
            after_due = send_pending_approval_reminders(
                now=now + dt.timedelta(minutes=59)
            )

        self.assertEqual(before_due["interval_minutes"], 120)
        self.assertEqual(before_due["sent"], 0)
        self.assertEqual(after_due["sent"], 1)
        self.assertEqual(sender.call_count, 1)

    def test_schema_upgrade_adds_persistent_reminder_timestamps(self):
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE orders (id INTEGER PRIMARY KEY, courier_type VARCHAR(10))"
            ))
            connection.execute(text(
                "CREATE TABLE batches (id INTEGER PRIMARY KEY, courier_type VARCHAR(10))"
            ))
            connection.execute(text(
                "CREATE TABLE manual_waybill_records (id INTEGER PRIMARY KEY, status VARCHAR(20))"
            ))
        upgrade_schema(engine)
        db_inspector = inspect(engine)
        for table in ("orders", "batches", "manual_waybill_records"):
            columns = {item["name"] for item in db_inspector.get_columns(table)}
            self.assertIn("approval_stage_started_at", columns)
            self.assertIn("approval_last_reminded_at", columns)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
