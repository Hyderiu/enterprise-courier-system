import io
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.main import app
from config.settings import settings
from database.connection import Base, get_db
from database import models


class AddressOnlyParserTests(unittest.TestCase):
    def test_infers_province_and_accepts_city_without_suffix(self):
        from utils.address_parser import parse_address

        result = parse_address("厦门思明区下堡路程163号2001室")
        self.assertTrue(result["success"])
        self.assertEqual(
            (result["province"], result["city"], result["district"]),
            ("福建省", "厦门市", "思明区"),
        )
        self.assertEqual(result["address"], "下堡路程163号2001室")

    def test_infers_province_from_full_city(self):
        from utils.address_parser import parse_address

        result = parse_address("杭州市滨江区正泰量测大厦1号楼2201")
        self.assertTrue(result["success"])
        self.assertEqual(
            (result["province"], result["city"], result["district"]),
            ("浙江省", "杭州市", "滨江区"),
        )
        self.assertEqual(result["address"], "正泰量测大厦1号楼2201")

    def test_unique_district_can_supply_missing_province_and_city(self):
        from utils.address_parser import parse_address

        result = parse_address("海淀区韦伯豪家园6号楼1单元602")
        self.assertTrue(result["success"])
        self.assertEqual(
            (result["province"], result["city"], result["district"]),
            ("北京市", "北京市", "海淀区"),
        )

    def test_ambiguous_district_is_rejected_instead_of_guessed(self):
        from utils.address_parser import parse_address

        result = parse_address("鼓楼区中山路1号")
        self.assertFalse(result["success"])
        self.assertIn("歧义", result["message"])

    def test_common_address_only_variants_are_normalized(self):
        from utils.address_parser import parse_address

        cases = [
            ("北京市朝阳区中源科技大厦3号楼9层", "北京市", "北京市", "朝阳区", "中源科技大厦3号楼9层"),
            ("济南市历城区凤凰国际南区3号楼2803", "山东省", "济南市", "历城区", "凤凰国际南区3号楼2803"),
            ("福州市晋安区远洋路一号", "福建省", "福州市", "晋安区", "远洋路一号"),
            ("广州市海珠区富力千禧商务中心11楼", "广东省", "广州市", "海珠区", "富力千禧商务中心11楼"),
        ]
        for raw, province, city, district, detail in cases:
            with self.subTest(raw=raw):
                result = parse_address(raw)
                self.assertTrue(result["success"], result)
                self.assertEqual((result["province"], result["city"], result["district"]), (province, city, district))
                self.assertEqual(result["address"], detail)

    def test_detail_repeating_city_name_is_not_erased(self):
        from utils.address_parser import parse_address

        result = parse_address("广东省中山市中山路1号")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["address"], "中山路1号")

    def test_historical_district_name_is_normalized(self):
        from utils.address_parser import parse_address

        result = parse_address("福建省漳州市长泰县马洋溪生态旅游区18栋202室")
        self.assertTrue(result["success"], result)
        self.assertEqual(
            (result["province"], result["city"], result["district"]),
            ("福建省", "漳州市", "长泰区"),
        )
        self.assertEqual(result["address"], "马洋溪生态旅游区18栋202室")


class FakeCourier:
    def __init__(self, fail_phone=None):
        self.fail_phone = fail_phone
        self.prechecked = []
        self.created = []

    def precheck_order(self, data):
        self.prechecked.append(data)
        if data["receiver_phone"] == self.fail_phone:
            return {"success": False, "message": "模拟校验失败"}
        return {"success": True, "message": "校验通过", "raw_response": {"created": False}}

    def create_order(self, data):
        self.created.append(data)
        return {
            "success": True,
            "courier_order_no": f"MOCK{len(self.created):04d}",
            "courier_order_nos": [f"MOCK{len(self.created):04d}"],
            "freight": 12.5,
        }


class BatchOrderFlowTests(unittest.TestCase):
    def setUp(self):
        self.original_login_enabled = settings.dingtalk_login_enabled
        settings.dingtalk_login_enabled = False
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)

        def override_db():
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        settings.dingtalk_login_enabled = self.original_login_enabled
        self.engine.dispose()

    @staticmethod
    def draft_payload():
        return {
            "courier_type": "SF", "express_type": "2", "express_type_name": "顺丰标快",
            "sender_name": "寄件人", "sender_phone": "13800000000",
            "sender_province": "福建省", "sender_city": "厦门市", "sender_district": "思明区",
            "sender_address": "示例路1号", "item_name": "文件", "item_weight": 1,
            "shipping_reason": "批量测试", "pickup_time_mode": "immediate",
            "estimated_freight": 12.5,
        }

    @staticmethod
    def workbook(rows):
        wb = Workbook(); ws = wb.active
        ws.append(["收件人姓名", "收件人电话", "收件人地址"])
        for row in rows: ws.append(row)
        buf = io.BytesIO(); wb.save(buf)
        return buf.getvalue()

    def create_and_import(self, rows):
        created = self.client.post("/api/batches/draft", json=self.draft_payload())
        self.assertEqual(created.status_code, 200)
        batch_id = created.json()["batch_id"]
        imported = self.client.post(
            f"/api/batches/{batch_id}/import",
            files={"file": ("recipients.xlsx", self.workbook(rows),
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        return batch_id, imported

    def test_template_contains_example_and_instructions(self):
        response = self.client.get("/api/batches/template")
        self.assertEqual(response.status_code, 200)
        wb = load_workbook(io.BytesIO(response.content), data_only=True)
        self.assertEqual(wb.sheetnames, ["收件人", "填写说明"])
        self.assertEqual(wb["收件人"]["A1"].value, "收件人姓名")
        self.assertEqual([cell.value for cell in wb["收件人"][1]], ["收件人姓名", "收件人电话", "收件人地址"])
        self.assertIn("单次最多 100", wb["填写说明"]["B6"].value)

    def test_recipient_file_can_be_uploaded_before_other_order_fields(self):
        created = self.client.post(
            "/api/batches/upload-draft", json={"debug_region_code": "xiamen_other"}
        )
        self.assertEqual(created.status_code, 200)
        batch_id = created.json()["batch_id"]
        imported = self.client.post(
            f"/api/batches/{batch_id}/import",
            files={"file": (
                "recipients.xlsx",
                self.workbook([["演示收件人甲", "13900000000", "厦门市思明区示例路1号"]]),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )},
        )
        self.assertEqual(imported.status_code, 200)
        self.assertTrue(imported.json()["can_commit"])

    def test_invalid_excel_row_blocks_the_whole_import(self):
        batch_id, imported = self.create_and_import([
            ["正确", "13900000000", "厦门湖里区金尚路1号"],
            ["错误", "", "厦门湖里区金尚路2号"],
        ])
        self.assertEqual(imported.status_code, 200)
        self.assertFalse(imported.json()["can_commit"])
        detail = self.client.get(f"/api/batches/{batch_id}").json()["batch"]
        self.assertIsNone(detail["details"])

    def test_prechecks_each_row_then_approval_places_each_order(self):
        rows = [
            ["演示收件人甲", "13900000000", "厦门湖里区金尚路1号"],
            ["演示收件人乙", "13700000000", "厦门市集美区杏林路2号"],
        ]
        batch_id, imported = self.create_and_import(rows)
        self.assertTrue(imported.json()["can_commit"])
        courier = FakeCourier()
        with patch("courier.get_courier", return_value=courier):
            committed = self.client.post(
                f"/api/batches/{batch_id}/commit",
                json={"batch_fields": self.draft_payload()},
            )
            self.assertEqual(committed.status_code, 200)
            self.assertTrue(committed.json()["ok"])
            self.assertEqual(committed.json()["prechecked"], 2)
            self.assertEqual(len(courier.prechecked), 2)
            self.assertEqual(len(courier.created), 0)

            submitted = self.client.post(f"/api/batches/{batch_id}/submit")
            self.assertEqual(submitted.status_code, 200)
            approved = self.client.post(f"/api/batches/{batch_id}/approve", json={})
            self.assertEqual(approved.status_code, 200)
            self.assertEqual(approved.json()["success"], 2)
            self.assertEqual(len(courier.created), 2)

        detail = self.client.get(f"/api/batches/{batch_id}").json()["batch"]
        self.assertEqual(detail["status"], "done")
        self.assertEqual([order["status"] for order in detail["orders"]], ["ordered", "ordered"])
        orders_page = self.client.get("/orders")
        self.assertNotIn("批量寄件 · 2 票", orders_page.text)
        self.assertIn("MOCK0001", orders_page.text)
        self.assertIn("MOCK0002", orders_page.text)
        approvals_page = self.client.get("/admin/approvals")
        self.assertIn("批量审批记录", approvals_page.text)

    def test_batch_quotes_can_be_refreshed_before_approval(self):
        rows = [["演示收件人甲", "13900000000", "厦门湖里区金尚路1号"]]
        batch_id, imported = self.create_and_import(rows)
        self.assertTrue(imported.json()["can_commit"])
        courier = FakeCourier()
        with patch("courier.get_courier", return_value=courier):
            committed = self.client.post(
                f"/api/batches/{batch_id}/commit",
                json={"batch_fields": self.draft_payload()},
            )
            self.assertEqual(committed.status_code, 200)
            refreshed = self.client.post(
                f"/api/batches/{batch_id}/quotes",
                json={"batch_fields": {
                    **self.draft_payload(),
                    "express_type_name": "顺丰标快（刷新）",
                }},
            )
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertTrue(refreshed.json()["ok"])
        self.assertEqual(len(refreshed.json()["tickets"]), 1)
        self.assertEqual(refreshed.json()["estimated_total"], 12.5)
        self.assertEqual(len(courier.prechecked), 2)

    def test_quote_only_batch_is_hidden_until_approval_submission(self):
        rows = [["演示收件人甲", "13900000000", "厦门湖里区金尚路1号"]]
        batch_id, imported = self.create_and_import(rows)
        self.assertTrue(imported.json()["can_commit"])
        courier = FakeCourier()
        with patch("courier.get_courier", return_value=courier):
            committed = self.client.post(
                f"/api/batches/{batch_id}/commit",
                json={"batch_fields": self.draft_payload()},
            )
            self.assertEqual(committed.status_code, 200)
            self.assertEqual(self.client.get("/api/orders").json()["items"], [])
            self.assertEqual(self.client.get("/api/orders/stats").json()["total"], 0)
            self.assertNotIn("13900000000", self.client.get("/orders").text)

            detail = self.client.get(f"/api/batches/{batch_id}").json()["batch"]
            self.assertEqual(detail["quote_total"], 12.5)
            self.assertEqual(detail["orders"][0]["freight"], 12.5)

            submitted = self.client.post(f"/api/batches/{batch_id}/submit")
            self.assertEqual(submitted.status_code, 200)
            self.assertEqual(len(self.client.get("/api/orders").json()["items"]), 1)
            self.assertIn("13900000000", self.client.get("/orders").text)

    def test_unshipped_failed_record_can_be_deleted(self):
        db = self.session_factory()
        order = models.Order(
            order_no="DELETE-TEST-001", courier_type="SF",
            sender_name="寄件人", sender_phone="13800000000",
            sender_address="测试寄件地址",
            receiver_name="收件人", receiver_phone="13900000000",
            receiver_province="福建省", receiver_city="厦门市",
            receiver_address="测试收件地址",
            item_name="文件", status="failed",
        )
        db.add(order); db.commit(); order_id = order.id; db.close()
        deleted = self.client.delete(f"/api/orders/{order_id}")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertTrue(deleted.json()["ok"])
        self.assertEqual(self.client.get(f"/api/orders/{order_id}").status_code, 404)

    def test_single_direct_order_is_disabled_even_with_confirmation(self):
        payload = {
            **self.draft_payload(),
            "receiver_name": "收件人", "receiver_phone": "13900000000",
            "receiver_province": "福建省", "receiver_city": "厦门市",
            "receiver_district": "湖里区", "receiver_address": "金尚路1号",
            "item_count": 1, "source_id": "direct-request-001",
        }
        rejected = self.client.post("/api/orders/direct", json=payload)
        self.assertEqual(rejected.status_code, 403)
        self.assertIn("禁用", rejected.json()["detail"])

        courier = FakeCourier()
        headers = {"X-Confirm-Production-Order": "DIRECT_ORDER_CONFIRMED"}
        with patch("courier.get_courier", return_value=courier):
            placed = self.client.post("/api/orders/direct", json=payload, headers=headers)
        self.assertEqual(placed.status_code, 403)
        self.assertEqual(len(courier.prechecked), 0)
        self.assertEqual(len(courier.created), 0)
        db = self.session_factory()
        try:
            self.assertEqual(db.query(models.Order).count(), 0)
        finally:
            db.close()

    def test_batch_direct_order_is_disabled(self):
        batch_id, imported = self.create_and_import([
            ["演示收件人甲", "13900000000", "厦门湖里区金尚路1号"],
            ["演示收件人乙", "13700000000", "厦门市集美区杏林路2号"],
        ])
        self.assertTrue(imported.json()["can_commit"])
        courier = FakeCourier()
        with patch("courier.get_courier", return_value=courier):
            committed = self.client.post(
                f"/api/batches/{batch_id}/commit",
                json={"batch_fields": self.draft_payload()},
            )
            self.assertTrue(committed.json()["ok"])
            rejected = self.client.post(f"/api/batches/{batch_id}/direct-order")
            self.assertEqual(rejected.status_code, 403)
            placed = self.client.post(
                f"/api/batches/{batch_id}/direct-order",
                headers={"X-Confirm-Production-Order": "DIRECT_ORDER_CONFIRMED"},
            )
        self.assertEqual(placed.status_code, 403)
        self.assertEqual(len(courier.created), 0)
        detail = self.client.get(f"/api/batches/{batch_id}").json()["batch"]
        self.assertEqual(detail["status"], "draft")
        self.assertTrue(all(order["status"] == "prechecked" for order in detail["orders"]))

    def test_landline_sender_phone_is_accepted(self):
        payload = self.draft_payload()
        payload["sender_phone"] = "010-12345678 转 123"
        response = self.client.post("/api/batches/draft", json=payload)
        self.assertEqual(response.status_code, 200)

    def test_empty_sender_contact_is_rejected(self):
        payload = self.draft_payload()
        payload["sender_phone"] = ""
        response = self.client.post("/api/batches/draft", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("联系电话不能为空", response.json()["detail"])
    def test_failed_precheck_cannot_enter_approval(self):
        batch_id, _ = self.create_and_import([
            ["演示收件人甲", "13900000000", "厦门湖里区金尚路1号"],
        ])
        courier = FakeCourier(fail_phone="13900000000")
        with patch("courier.get_courier", return_value=courier):
            committed = self.client.post(f"/api/batches/{batch_id}/commit", json={})
        self.assertFalse(committed.json()["ok"])
        submitted = self.client.post(f"/api/batches/{batch_id}/submit")
        self.assertEqual(submitted.status_code, 400)
        self.assertIn("未通过预下单校验", submitted.json()["detail"])


if __name__ == "__main__":
    unittest.main()
