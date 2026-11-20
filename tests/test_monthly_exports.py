import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from config.settings import settings
from database import models
from database.connection import Base
from utils.monthly_exports import (
    backfill_monthly_export_files, ensure_monthly_exports,
    generate_monthly_exports, list_monthly_exports, monthly_export_path,
    previous_month,
)
from api.main import monthly_export_health


class MonthlyExportTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.admin = models.User(
            dingtalk_userid="admin-1", name="管理员", role="admin", is_active=True
        )
        self.other_admin = models.User(
            dingtalk_userid="admin-2", name="管理员二", role="admin_approver", is_active=True
        )
        self.employee = models.User(
            dingtalk_userid="employee-1", name="员工", role="user", is_active=True
        )
        self.db.add_all([self.admin, self.other_admin, self.employee])
        self.db.flush()
        self.db.add_all([
            self._order("SF-SEP", "SF", datetime(2026, 9, 10, 9), self.employee.id),
            self._order("DB-SEP", "DB", datetime(2026, 9, 30, 23, 59), self.admin.id),
            self._order(
                "JDL-SEP", "JDL", datetime(2026, 9, 20, 12), self.employee.id,
                region_code="chengdu", express_type_name="京东标快",
            ),
            self._order("SF-OCT", "SF", datetime(2026, 10, 1, 0), self.admin.id),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _order(
        number, courier, created_at, creator,
        region_code="xiamen_other", express_type_name="",
    ):
        return models.Order(
            order_no=number, courier_type=courier, courier_order_no=f"WB-{number}",
            sender_name="寄件人", sender_phone="13800000000", sender_address="软件园",
            receiver_name="收件人", receiver_phone="13900000000",
            receiver_province="福建省", receiver_city="厦门市", receiver_address="测试路",
            item_name="文件", cost_type="department", dept_name="行政部",
            status="ordered", created_by=creator, created_at=created_at,
            region_code=region_code, express_type_name=express_type_name,
        )

    def test_previous_month_handles_year_boundary(self):
        self.assertEqual(previous_month(datetime(2026, 1, 3)), "2025-12")

    def test_generates_two_configured_workbooks_for_exact_calendar_month(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = generate_monthly_exports(self.db, "2026-09", directory)
            self.assertEqual(manifest["files"]["SF"]["count"], 1)
            self.assertEqual(manifest["files"]["DB"]["count"], 1)
            sf_path, filename = monthly_export_path("2026-09", "SF", directory)
            self.assertEqual(filename, "顺丰寄件详情-2026年09月.xlsx")
            workbook = load_workbook(sf_path, read_only=True, data_only=True)
            self.assertEqual(
                workbook.sheetnames,
                ["全部区域顺丰-部门", "全部区域顺丰-项目"],
            )
            self.assertEqual(workbook["全部区域顺丰-部门"].max_row, 2)
            self.assertEqual(manifest["schema_version"], 2)
            workbook.close()
            self.assertEqual(list_monthly_exports(directory)[0]["month"], "2026-09")

    def test_region_and_carrier_exports_have_distinct_sheet_names_and_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = generate_monthly_exports(self.db, "2026-09", directory)
            self.assertEqual(manifest["files"]["xiamen_other-SF"]["count"], 1)
            self.assertEqual(manifest["files"]["xiamen_other-DB"]["count"], 1)
            self.assertEqual(manifest["files"]["xiamen_other-JDL"]["count"], 0)
            self.assertEqual(manifest["files"]["chengdu-SF"]["count"], 0)
            self.assertEqual(manifest["files"]["chengdu-DB"]["count"], 0)
            self.assertEqual(manifest["files"]["chengdu-JDL"]["count"], 1)

            for code, carrier_name in (("SF", "顺丰"), ("DB", "德邦"), ("JDL", "京东")):
                path, _ = monthly_export_path(
                    "2026-09", code, directory, region_code="all"
                )
                workbook = load_workbook(path, read_only=True, data_only=True)
                self.assertEqual(
                    workbook.sheetnames,
                    [f"全部区域{carrier_name}-部门", f"全部区域{carrier_name}-项目"],
                )
                department = workbook[f"全部区域{carrier_name}-部门"]
                self.assertEqual(department.max_row, 2)
                headers = [cell.value for cell in department[1]]
                product_column = headers.index("快递产品") + 1
                shown_product = str(department.cell(2, product_column).value or "")
                self.assertIn(carrier_name, shown_product)
                for other in {"顺丰", "德邦", "京东"} - {carrier_name}:
                    self.assertNotIn(other, shown_product)
                workbook.close()

    def test_notifies_all_active_admins_once_and_never_regular_employees(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(settings, "public_base_url", "https://express.example"), \
             patch("utils.monthly_exports.DingTalkClient.send_work_notification") as send:
            send.return_value = {"task_id": 123}
            first = ensure_monthly_exports(
                month="2026-09", db=self.db, output_dir=directory
            )
            second = ensure_monthly_exports(
                month="2026-09", db=self.db, output_dir=directory
            )
            self.assertTrue(first["notification"]["sent"])
            self.assertTrue(second["notification"]["sent"])
            self.assertEqual(send.call_count, 2)
            recipient_ids = {call.args[0] for call in send.call_args_list}
            self.assertEqual(recipient_ids, {"admin-1", "admin-2"})
            for call in send.call_args_list:
                self.assertIn("2026年09月", call.args[2])
                self.assertIn("%2Fdingtalk-notification-entry%3Ftoken%3D", call.args[4])
            self.assertEqual(len(first["notification"]["sent_recipient_keys"]), 2)

    def test_rebuilding_a_missing_file_preserves_sent_notification(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(settings, "public_base_url", "https://express.example"), \
             patch("utils.monthly_exports.DingTalkClient.send_work_notification") as send:
            send.return_value = {"task_id": 123}
            first = ensure_monthly_exports(month="2026-09", db=self.db, output_dir=directory)
            missing = Path(directory) / "2026-09" / first["files"]["all-SF"]["filename"]
            missing.unlink()
            rebuilt = ensure_monthly_exports(month="2026-09", db=self.db, output_dir=directory)
            self.assertTrue(rebuilt["notification"]["sent"])
            self.assertEqual(send.call_count, 2)

    def test_retry_skips_an_admin_who_already_received_the_notice(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(settings, "public_base_url", "https://express.example"), \
             patch("utils.monthly_exports.DingTalkClient.send_work_notification") as send:
            send.side_effect = [{"task_id": 1}, RuntimeError("temporary"), {"task_id": 2}]
            failed = ensure_monthly_exports(month="2026-09", db=self.db, output_dir=directory)
            self.assertFalse(failed["notification"]["sent"])
            self.assertEqual(len(failed["notification"]["sent_recipient_keys"]), 1)
            recovered = ensure_monthly_exports(month="2026-09", db=self.db, output_dir=directory)
            self.assertTrue(recovered["notification"]["sent"])
            self.assertEqual(send.call_count, 3)
            self.assertEqual(send.call_args_list[0].args[0], "admin-1")
            self.assertEqual(send.call_args_list[2].args[0], "admin-2")

    def test_startup_backfill_restores_files_without_sending_notice(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("utils.monthly_exports.DingTalkClient.send_work_notification") as send:
            first = backfill_monthly_export_files(
                month="2026-09", db=self.db, output_dir=directory
            )
            second = backfill_monthly_export_files(
                month="2026-09", db=self.db, output_dir=directory
            )
            self.assertEqual(first["month"], "2026-09")
            self.assertEqual(second["month"], "2026-09")
            self.assertFalse((second.get("notification") or {}).get("sent", False))
            self.assertTrue(second["notification"]["suppressed"])
            ensure_monthly_exports(month="2026-09", db=self.db, output_dir=directory)
            send.assert_not_called()

    def test_scheduler_notifies_monthly_and_only_backfills_files_on_startup(self):
        source = Path("scheduler/scheduler.py").read_text(encoding="utf-8")
        self.assertIn("CronTrigger(day=1, hour=10, minute=0)", source)
        monthly_block = source.split("run_monthly_exports,", 1)[1].split(")\n    _scheduler.start", 1)[0]
        self.assertIn("monthly_exports_startup_backfill", monthly_block)
        self.assertIn("next_run_time", monthly_block)

    def test_public_health_only_exposes_safe_counts(self):
        manifest = {
            "month": "2026-09",
            "files": {"SF": {"count": 12}, "DB": {"count": 7}},
            "notification": {"sent": True, "recipient_count": 3, "task_id": 888},
        }
        with patch("api.main.list_monthly_exports", return_value=[manifest]):
            result = monthly_export_health()
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["sf_count"], 12)
        self.assertEqual(result["recipient_count"], 3)
        self.assertNotIn("task_id", result)


if __name__ == "__main__":
    unittest.main()
