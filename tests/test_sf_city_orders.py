import unittest
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.routes.sf_city_orders import (
    PRODUCT_TYPES, REGION_PRODUCT_TYPES, SFCityOrderCreate, SFCityQuote, _friendly_sf_city_error,
    _precheck_with_transient_retry, create_order,
)
from database import models
from database.connection import Base
from utils.sf_city_access import sf_city_user_allowed


class SFCityOrderRouteTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.reviewer = models.User(
            name="二级审核员", dingtalk_userid="reviewer", role="admin",
        )
        self.user = models.User(
            name="员工", dingtalk_userid="employee", role="user",
            department="示例科技有限公司-运营中心", phone="13800000009",
            work_location="厦门", region_code="xiamen_other",
        )
        self.db.add_all([self.reviewer, self.user])
        self.db.flush()
        self.db.add_all([
            models.SystemConfig(config_key="approval_level_count", config_value="2"),
            models.SystemConfig(config_key="approval_level1_enabled", config_value="0"),
            models.SystemConfig(
                config_key="approval_level2_user_id", config_value=str(self.reviewer.id)
            ),
            models.SystemConfig(
                config_key="region.xiamen_other.sf_city_live_order_enabled",
                config_value="1",
            ),
            models.SystemConfig(
                config_key="region.xiamen_other.sf_city_shop_id",
                config_value="9999999999001",
            ),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_quote_defaults_weight_to_one_kg(self):
        body = SFCityQuote(
            receiver_address="示例园区", receiver_lng="118.10000",
            receiver_lat="24.490625",
        )
        self.assertEqual(body.item_weight, 1)
        self.assertEqual(body.item_value, 0.01)
        self.assertEqual(body.insurance_amount, 0)
        self.assertEqual(body.product_type, 10)

    def test_coordinate_from_map_tool_is_accepted_without_changing_precision(self):
        body = SFCityQuote(
            receiver_address="北京示例园区",
            receiver_city="北京市", receiver_lng="116.36",
            receiver_lat="40.00", product_type=12,
        )
        self.assertEqual(body.receiver_lng, "116.36")
        self.assertEqual(body.receiver_lat, "40.00")

    def test_xiamen_quote_rejects_an_obvious_beijing_coordinate_before_api_call(self):
        from api.routes.sf_city_orders import _quote_data

        body = SFCityQuote(
            receiver_address="北京示例园区",
            receiver_city="厦门市", receiver_lng="116.36",
            receiver_lat="40.00", product_type=10,
        )
        with self.assertRaisesRegex(Exception, "当前收件定位不在厦门市"):
            _quote_data(body, "xiamen_other")

    def test_receiver_landline_is_allowed_for_government_offices(self):
        body = SFCityOrderCreate(
            sender_name="申请员工", sender_phone="13800000009",
            receiver_name="政府收发室", receiver_phone="0592-1234567",
            receiver_address="示例园区", receiver_lng="118.10000",
            receiver_lat="24.490625", item_name="文件",
            shipping_reason="公文送达", dept_name="运营中心",
        )
        self.assertEqual(body.receiver_phone, "0592-1234567")

    def test_other_product_category_is_supported(self):
        body = SFCityQuote(
            receiver_address="示例园区", receiver_lng="118.10000",
            receiver_lat="24.490625", product_type=99,
        )
        self.assertEqual(body.product_type, 99)

    def test_product_types_match_sf_city_enum(self):
        self.assertEqual(PRODUCT_TYPES, {
            10: "快件", 36: "证照", 3: "百货", 12: "文件",
        })
        self.assertEqual(REGION_PRODUCT_TYPES["xiamen_other"], {10: "快件", 36: "证照"})
        self.assertEqual(REGION_PRODUCT_TYPES["beijing"], {3: "百货", 12: "文件"})

    def test_upstream_delivery_errors_are_translated_for_employees(self):
        message = _friendly_sf_city_error(
            RuntimeError("顺丰同城请求失败 1103: 收件地址超出配送范围"),
            "报价",
        )
        self.assertIn("该收件地址暂不在顺丰同城配送范围内", message)
        self.assertIn("顺丰错误码：1103", message)

    def test_no_rider_is_not_misreported_as_an_address_problem(self):
        message = _friendly_sf_city_error(
            RuntimeError("顺丰同城请求失败 2108: 当前暂无运力"),
            "报价",
        )
        self.assertIn("暂时没有可用骑士", message)
        self.assertNotIn("不在顺丰同城配送范围", message)

    def test_unavailable_shop_product_type_has_specific_guidance(self):
        message = _friendly_sf_city_error(
            RuntimeError("顺丰同城请求失败 1015: 店铺未配置当前产品类型或品类"),
            "报价",
        )
        self.assertIn("店铺未开通所选物品类别", message)
        self.assertIn("顺丰错误码：1015", message)
        self.assertNotIn("月结配置暂不可用", message)

    def test_unknown_upstream_errors_hide_technical_details(self):
        message = _friendly_sf_city_error(
            RuntimeError("顺丰同城请求失败 9999: NullPointerException"),
            "报价",
        )
        self.assertIn("暂时无法完成报价", message)
        self.assertIn("顺丰错误码：9999", message)
        self.assertNotIn("NullPointerException", message)

    def test_code_1004_suggests_retry_before_reentering_all_fields(self):
        message = _friendly_sf_city_error(
            RuntimeError("顺丰同城请求失败 1004: 订单信息校验失败"),
            "报价",
        )
        self.assertIn("先重新获取报价", message)
        self.assertIn("区县、详细地址与定位一致", message)
        self.assertIn("顺丰错误码：1004", message)

    def test_code_1004_is_retried_once_before_displaying_an_error(self):
        client = Mock()
        client.precheck_order.side_effect = [
            RuntimeError("顺丰同城请求失败 1004: 订单信息校验失败"),
            {"success": True, "estimated_fee": 35.2},
        ]
        result = _precheck_with_transient_retry(client, {"receiver_city": "北京市"})
        self.assertTrue(result["success"])
        self.assertEqual(client.precheck_order.call_count, 2)

    @patch(
        "api.routes.sf_city_orders.queue_approval_progress_notification",
        return_value={"queued": True},
    )
    @patch("api.routes.sf_city_orders.queue_approval_notification", return_value={"queued": True})
    @patch("api.routes.sf_city_orders._client")
    def test_submission_saves_shop_sender_coordinates_and_waits_for_level2(
        self, get_client, _notification, applicant_notification,
    ):
        client = Mock()
        client.shop_id = "9999999999001"
        client.precheck_order.return_value = {
            "success": True, "estimated_fee": 5.6,
            "promise_delivery_minutes": 30,
        }
        client.shop_info.return_value = {
            "shop_name": "示例科技有限公司",
            "shop_contact_name": "店铺联系人",
            "shop_contact_phone": "13600000000",
            "shop_address": "福建省厦门市思明区示例大道100号A座301室",
        }
        get_client.return_value = client
        request = SimpleNamespace(state=SimpleNamespace(
            current_user={
                "id": self.user.id, "name": self.user.name, "role": "employee",
                "department": self.user.department, "region_code": "xiamen_other",
            },
            auth_enabled=True,
        ))
        body = SFCityOrderCreate(
            sender_name="申请员工", sender_phone="13800000009",
            receiver_name="收件人", receiver_phone="13900000000",
            receiver_address="示例大道100号A座401室",
            receiver_lng="118.10000", receiver_lat="24.490625",
            item_name="文件", shipping_reason="合同送签",
            item_remark="到达后联系前台", insurance_amount=500,
            dept_code="D1", dept_name=self.user.department,
        )
        result = create_order(body, request, BackgroundTasks(), self.db)
        saved = self.db.query(models.Order).filter_by(id=result["id"]).one()
        self.assertEqual(saved.courier_type, "SF_CITY")
        self.assertEqual(saved.sender_name, "申请员工")
        self.assertEqual(saved.sender_phone, "13800000009")
        self.assertEqual(saved.receiver_lng, "118.10000")
        self.assertEqual(saved.receiver_lat, "24.490625")
        self.assertEqual(saved.item_weight, 1)
        self.assertEqual(saved.item_remark, "到达后联系前台")
        self.assertEqual(saved.insurance, 500)
        self.assertEqual(saved.freight, 5.6)
        self.assertEqual(saved.status, "pending")
        self.assertEqual(saved.approval_current_level, 2)
        self.assertEqual(saved.approval_level2_user_id, self.reviewer.id)
        self.assertEqual(json.loads(saved.order_response)["stage"], "precheck")
        client.precheck_order.assert_called_once()
        client.create_order.assert_not_called()
        applicant_notification.assert_called_once()
        self.assertEqual(applicant_notification.call_args.args[3], "submitted")

    def test_sf_city_rollout_allows_xiamen_but_not_other_employees(self):
        self.assertTrue(sf_city_user_allowed(
            self.db, {"id": self.user.id}, auth_enabled=True
        ))
        other = models.User(
            name="外地员工", dingtalk_userid="employee-other", role="user",
            department="运营中心", work_location="泉州",
            region_code="xiamen_other",
        )
        self.db.add(other)
        self.db.commit()
        self.assertFalse(sf_city_user_allowed(
            self.db, {"id": other.id}, auth_enabled=True
        ))

    def test_sf_city_rollout_allows_beijing_only_with_beijing_region(self):
        beijing = models.User(
            name="北京员工", dingtalk_userid="employee-beijing", role="user",
            department="示例科技有限公司-北京团队",
            work_location="北京", region_code="beijing",
        )
        self.db.add(beijing)
        self.db.commit()
        self.assertTrue(sf_city_user_allowed(
            self.db, {"id": beijing.id}, auth_enabled=True, region_code="beijing",
        ))
        self.assertFalse(sf_city_user_allowed(
            self.db, {"id": beijing.id}, auth_enabled=True, region_code="xiamen_other",
        ))

    @patch("api.routes.sf_city_orders.queue_approval_progress_notification")
    @patch("api.routes.sf_city_orders.queue_approval_notification")
    @patch("api.routes.sf_city_orders._client")
    def test_city_order_uses_same_optional_level1_policy_as_express(
        self, get_client, _approval_notice, _applicant_notice,
    ):
        supervisor = models.User(
            name="直属主管", dingtalk_userid="supervisor", role="user",
            department="示例科技有限公司-运营中心",
            work_location="厦门", region_code="xiamen_other",
        )
        self.db.add(supervisor)
        self.db.flush()
        self.user.supervisor_user_id = supervisor.id
        self.user.approval_level1_required_override = True
        self.db.query(models.SystemConfig).filter_by(
            config_key="approval_level1_enabled"
        ).one().config_value = "1"
        self.db.commit()

        client = Mock()
        client.shop_id = "9999999999001"
        client.precheck_order.return_value = {
            "success": True, "estimated_fee": 8.8,
        }
        client.shop_info.return_value = {"shop_address": "厦门固定取件地址"}
        get_client.return_value = client
        request = SimpleNamespace(state=SimpleNamespace(
            current_user={
                "id": self.user.id, "name": self.user.name,
                "department": self.user.department,
            },
            auth_enabled=True,
        ))
        body = SFCityOrderCreate(
            sender_name="申请员工", sender_phone="13800000009",
            receiver_name="收件人", receiver_phone="13900000000",
            receiver_address="厦禾路900号", receiver_lng="118.11690",
            receiver_lat="24.46840", item_name="文件",
            shipping_reason="合同送签", dept_name=self.user.department,
        )

        result = create_order(body, request, BackgroundTasks(), self.db)
        saved = self.db.query(models.Order).filter_by(id=result["id"]).one()
        self.assertEqual(saved.status, "pending")
        self.assertTrue(saved.approval_level1_required)
        self.assertEqual(saved.approval_current_level, 1)
        self.assertEqual(saved.approval_level1_user_id, supervisor.id)
        self.assertEqual(saved.approval_level2_user_id, self.reviewer.id)
        client.create_order.assert_not_called()

    def test_non_xiamen_destination_is_rejected_before_precheck(self):
        body = SFCityQuote(
            receiver_city="泉州市", receiver_address="测试地址",
            receiver_lng="118.67000", receiver_lat="24.88000",
        )
        from api.routes.sf_city_orders import _quote_data
        with self.assertRaisesRegex(Exception, "仅支持厦门市内配送"):
            _quote_data(body)

    def test_beijing_destination_uses_beijing_region_validation(self):
        from api.routes.sf_city_orders import _quote_data
        body = SFCityQuote(
            receiver_city="北京市", receiver_address="朝阳区东坝街道",
            receiver_lng="116.55000", receiver_lat="39.96000", product_type=12,
        )
        payload = _quote_data(body, "beijing")
        self.assertEqual(payload["receiver_city"], "北京市")
        with self.assertRaisesRegex(Exception, "仅支持厦门市内配送"):
            _quote_data(body, "xiamen_other")


if __name__ == "__main__":
    unittest.main()
