import base64
import datetime as dt
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from config.settings import settings
from courier.deppon import DepponCourier
from courier.sf import SFCourier
from courier_services import SERVICE_MAP


ORDER = {
    "order_no": "CO202609110001",
    "sender_name": "发件人",
    "sender_phone": "13800000000",
    "sender_company": "测试公司",
    "sender_province": "福建省",
    "sender_city": "厦门市",
    "sender_district": "思明区",
    "sender_address": "示例路1号",
    "receiver_name": "收件人",
    "receiver_phone": "13900000000",
    "receiver_province": "福建省",
    "receiver_city": "厦门市",
    "receiver_district": "湖里区",
    "receiver_address": "测试路2号",
    "item_name": "文件",
    "item_weight": 1.2,
    "item_count": 1,
}


class SFAdapterTests(unittest.TestCase):
    def test_beijing_only_sf_products_are_declared(self):
        self.assertEqual(SERVICE_MAP["sf_same_day"]["express_type"], "6")
        self.assertEqual(SERVICE_MAP["sf_same_day"]["regions"], {"beijing"})
        self.assertFalse(SERVICE_MAP["sf_same_day"]["catalog_enabled"])
        self.assertEqual(SERVICE_MAP["sf_large"]["express_type"], "255")
        self.assertEqual(SERVICE_MAP["sf_large"]["name"], "顺丰大件")
        self.assertEqual(SERVICE_MAP["sf_large"]["express_name"], "顺丰卡航")
        self.assertIn("适合约20kg及以上", SERVICE_MAP["sf_large"]["difference"])
        self.assertNotIn("底层", SERVICE_MAP["sf_large"]["difference"])
        self.assertNotIn("编码255", SERVICE_MAP["sf_large"]["difference"])

    def test_create_payload_uses_current_field_names_and_calls_courier(self):
        client = SFCourier()
        payload = client._build_create_order_payload({**ORDER, "express_type": "2"})
        self.assertEqual(payload["language"], "zh-CN")
        self.assertEqual(payload["isDocall"], 1)
        self.assertEqual(payload["cargoDetails"][0]["name"], "文件")
        self.assertNotIn("cargoName", payload["cargoDetails"][0])
        self.assertEqual(payload["contactInfoList"][0]["mobile"], "13800000000")
        self.assertNotIn("tel", payload["contactInfoList"][0])

    def test_create_payload_includes_volume_and_insurance(self):
        client = SFCourier()
        payload = client._build_create_order_payload({
            **ORDER, "express_type": "2", "item_volume": 0.012, "insurance_amount": 3000,
        })
        self.assertEqual(payload["totalVolume"], 12000.0)
        self.assertEqual(payload["serviceList"], [{"name": "INSURE", "value": "3000"}])

    def test_multi_parcel_payload_requests_mother_and_child_waybills(self):
        client = SFCourier()
        payload = client._build_create_order_payload({**ORDER, "express_type": "2", "parcel_count": 3})
        self.assertEqual(payload["parcelQty"], 3)
        self.assertEqual(client._build_pre_order_payload({**ORDER, "express_type": "2", "parcel_count": 3})["parcelQty"], 3)

    def test_appointment_uses_documented_pickup_window_fields(self):
        client = SFCourier()
        payload = client._build_create_order_payload({
            **ORDER,
            "express_type": "2",
            "pickup_time_mode": "appointment",
            "pickup_start_time": "2026-09-12T09:00",
            "pickup_end_time": "2026-09-12T11:00",
        })
        self.assertEqual(payload["sendStartTm"], "2026-09-12 09:00:00")
        self.assertEqual(payload["extraInfoList"], [{
            "attrName": "pickupAppointEndTime", "attrVal": "2026-09-12 11:00:00"
        }])

    def test_delivery_quote_uses_future_appointment_for_eta(self):
        client = SFCourier(partner_id="PARTNER", check_word="CHECK")
        payload = client._build_delivery_quote_payload({
            **ORDER,
            "pickup_start_time": "2099-12-31T08:00",
        })
        self.assertEqual(payload["consignedTime"], "2099-12-31 08:00:00")

    def test_precheck_calls_pre_order_without_creating_an_order(self):
        client = SFCourier(partner_id="PARTNER", check_word="CHECK")
        response = {
            "apiResultCode": "A1000",
            "apiResultData": json.dumps({"success": True, "errorCode": "S0000", "msgData": {}}),
        }
        with patch.object(client, "_request", return_value=response) as request:
            result = client.precheck_order({**ORDER, "express_type": "2"})
        self.assertTrue(result["success"])
        self.assertEqual(request.call_args.args[0], "EXP_RECE_PRE_ORDER")
        self.assertNotIn("isGenWaybillNo", request.call_args.args[1])

    def test_gateway_preview_is_signed_but_never_exposes_checkword(self):
        client = SFCourier(partner_id="PARTNER", check_word="TOP_SECRET")
        preview = client.build_gateway_preview("EXP_RECE_PRE_ORDER", {"orderId": "CO1"})
        self.assertEqual(preview["form"]["serviceCode"], "EXP_RECE_PRE_ORDER")
        self.assertEqual(preview["form"]["partnerID"], "PARTNER")
        self.assertNotIn("TOP_SECRET", json.dumps(preview))
        expected_json = json.dumps({"orderId": "CO1", "language": "zh-CN"}, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(preview["form"]["msgDigest"], client._sign(expected_json, preview["form"]["timestamp"]))

    def test_delivery_quote_payload_and_response_are_normalized(self):
        client = SFCourier(partner_id="PARTNER", check_word="CHECK")
        response = {
            "apiResultCode": "A1000",
            "apiResultData": json.dumps({
                "success": True, "errorCode": "S0000",
                "msgData": {"deliverTmDto": [
                    {"businessType": "1", "businessTypeDesc": "顺丰特快",
                     "deliverTime": "2026-09-16 18:00:00,2026-09-16 18:00:00", "fee": None},
                    {"businessType": "2", "businessTypeDesc": "顺丰标快",
                     "deliverTime": "2026-09-16 12:00:00,2026-09-16 16:00:00", "fee": 12},
                    {"businessType": "6", "businessTypeDesc": "顺丰即日",
                     "deliverTime": "2026-09-15 20:00:00,2026-09-15 22:00:00", "fee": 28},
                ]},
            }),
        }
        with patch.object(client, "_request", return_value=response) as request:
            result = client.query_delivery_quotes({**ORDER, "item_volume": 0.006})
        self.assertTrue(result["success"])
        payload = request.call_args.args[1]
        self.assertEqual(request.call_args.args[0], "EXP_RECE_QUERY_DELIVERTM")
        self.assertEqual(payload["businessType"], "")
        self.assertEqual(payload["searchPrice"], "1")
        self.assertEqual(payload["volume"], 6000)
        self.assertIn("福建省厦门市思明区", payload["srcAddress"]["address"])
        self.assertEqual(result["items"][0]["estimated_delivery"], "2026-09-16 18:00:00")
        self.assertEqual(result["items"][1]["estimated_delivery"], "2026-09-16 12:00:00 至 2026-09-16 16:00:00")
        self.assertEqual(result["items"][1]["estimated_fee"], 12.0)
        self.assertEqual(result["items"][2]["business_type"], "6")
        self.assertEqual(result["items"][2]["estimated_fee"], 28.0)

    def test_single_delivery_quote_object_is_supported(self):
        client = SFCourier(partner_id="PARTNER", check_word="CHECK")
        response = {
            "apiResultCode": "A1000",
            "apiResultData": json.dumps({
                "success": True, "errorCode": "S0000",
                "msgData": {"deliverTmDto": {
                    "businessType": "1", "businessTypeDesc": "顺丰特快",
                    "deliverTime": "2026-09-16 18:00:00", "fee": 22,
                }},
            }),
        }
        with patch.object(client, "_request", return_value=response):
            result = client.query_delivery_quotes(ORDER)
        self.assertTrue(result["success"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["business_type"], "1")
        self.assertEqual(result["items"][0]["estimated_fee"], 22.0)

    def test_gateway_and_inner_business_response_are_both_parsed(self):
        outer = {
            "apiErrorMsg": "",
            "apiResultCode": "A1000",
            "apiResultData": json.dumps({
                "success": True,
                "errorCode": "S0000",
                "errorMsg": None,
                "msgData": {"orderId": "CO1", "waybillNoInfoList": [{"waybillNo": "SF123"}]},
            }),
        }
        ok, message, data, code = SFCourier._business_result(outer)
        self.assertTrue(ok)
        self.assertEqual(code, "S0000")
        self.assertEqual(data["waybillNoInfoList"][0]["waybillNo"], "SF123")
        self.assertEqual(message, "成功")

    def test_http_200_with_inner_failure_is_not_success(self):
        outer = {
            "apiResultCode": "A1000",
            "apiResultData": json.dumps({"success": False, "errorCode": "8019", "errorMsg": "订单已确认或已消单"}),
        }
        ok, message, _, code = SFCourier._business_result(outer)
        self.assertFalse(ok)
        self.assertEqual(code, "8019")
        self.assertIn("消单", message)

    def test_order_result_sub_waybill_and_fee_request_formats(self):
        client = SFCourier(partner_id="PARTNER", check_word="CHECK")
        ok_response = {
            "apiResultCode": "A1000",
            "apiResultData": json.dumps({"success": True, "errorCode": "S0000", "msgData": {
                "waybillNoInfoList": [{"waybillType": 2, "waybillNo": "SF-CHILD"}],
                "waybillFeeList": [{"type": "1", "value": 12}, {"type": "3", "value": 2}],
            }}),
        }
        with patch.object(client, "_request", return_value=ok_response) as request:
            searched = client.query_order_result("CO1")
            self.assertTrue(searched["success"])
            self.assertEqual(request.call_args.args, ("EXP_RECE_SEARCH_ORDER_RESP", {
                "searchType": "1", "orderId": "CO1", "language": "zh-CN",
            }))
            children = client.get_sub_waybills("CO1", 1)
            self.assertEqual(children["waybill_numbers"], ["SF-CHILD"])
            self.assertEqual(request.call_args.args, ("EXP_RECE_GET_SUB_MAILNO", {"orderId": "CO1", "parcelQty": 1}))
            with patch.object(settings, "sf_mock_mode", False):
                fee = client.query_actual_freight("SF123")
            self.assertEqual(fee["freight"], 14.0)
            self.assertEqual(request.call_args.args, ("EXP_RECE_QUERY_SFWAYBILL", {
                "trackingType": "2", "trackingNum": "SF123",
            }))

    def test_create_order_extracts_waybill_from_real_response_shape(self):
        client = SFCourier()
        client.partner_id = "PARTNER"
        client.check_word = "CHECK"
        response = {
            "apiResultCode": "A1000",
            "apiResultData": json.dumps({
                "success": True, "errorCode": "S0000",
                "msgData": {
                    "filterResult": 2,
                    "waybillNoInfoList": [
                        {"waybillType": 2, "waybillNo": "SF1234567891"},
                        {"waybillType": 1, "waybillNo": "SF1234567890"},
                    ],
                },
            }),
        }
        with patch.object(settings, "sf_mock_mode", False), \
             patch.object(settings, "sf_live_order_enabled", True), \
             patch.object(client, "_request", return_value=response):
            result = client.create_order({**ORDER, "express_type": "2"})
        self.assertTrue(result["success"])
        self.assertEqual(result["courier_order_no"], "SF1234567890")
        self.assertEqual(result["courier_order_nos"], ["SF1234567890", "SF1234567891"])


class DepponAdapterTests(unittest.TestCase):
    def configured_client(self):
        client = DepponCourier()
        client.company_code = "COMPANY"
        client.customer_code = "MONTHLY"
        client.appkey = "APPKEY"
        client.sign_value = "SIGN"
        return client

    def test_digest_matches_verified_algorithm(self):
        client = self.configured_client()
        params = '{"a":1}'
        timestamp = "1788888888000"
        expected_md5 = hashlib.md5((params + "APPKEY" + timestamp).encode()).hexdigest().lower()
        self.assertEqual(client._sign(params, timestamp), base64.b64encode(expected_md5.encode()).decode())

    def test_create_payload_is_pickup_monthly_and_no_trace_subscription(self):
        client = self.configured_client()
        payload = client._build_order_params({**ORDER, "express_type": "XJBK"})
        self.assertEqual(payload["orderType"], "1")
        self.assertEqual(payload["needTraceInfo"], 2)
        self.assertEqual(payload["transportType"], "XJBK")
        self.assertEqual(payload["payType"], "2")
        self.assertEqual(payload["customerCode"], "MONTHLY")
        self.assertEqual(payload["packageInfo"]["deliveryType"], "3")
        self.assertEqual(payload["sender"]["county"], "思明区")
        self.assertTrue(payload["logisticID"].startswith("SIGN"))

    def test_create_payload_includes_volume_and_full_insurance(self):
        client = self.configured_client()
        payload = client._build_order_params({
            **ORDER, "express_type": "XJTK", "item_volume": 0.012, "insurance_amount": 3000,
        })
        self.assertEqual(payload["packageInfo"]["totalVolume"], 0.012)
        self.assertEqual(payload["addServices"], {"insuranceType": "0", "insuranceValue": 3000.0})

    def test_scattered_pickup_rejects_multiple_packages(self):
        client = self.configured_client()
        with self.assertRaisesRegex(ValueError, "不支持子母件"):
            client._build_order_params({**ORDER, "parcel_count": 2, "express_type": "XJBK"})

    def test_request_wrapper_only_sends_verified_public_fields(self):
        client = self.configured_client()
        fake = Mock()
        fake.raise_for_status.return_value = None
        fake.json.return_value = {"result": "true", "resultCode": "1000"}
        with patch("courier.deppon.requests.post", return_value=fake) as mocked_post:
            result = client._request("https://example.test/api", {"companyCode": "COMPANY", "logisticID": "SIGN1"})
        self.assertEqual(result["resultCode"], "1000")
        form = mocked_post.call_args.kwargs["data"]
        self.assertEqual(set(form), {"companyCode", "digest", "timestamp", "params"})
        self.assertNotIn("customerCode", form)
        self.assertNotIn("logisticID", form)

    def test_create_order_extracts_waybill_from_verified_response_shape(self):
        client = self.configured_client()
        response = {
            "result": "true", "reason": "成功", "resultCode": "1000",
            "mailNo": "DPK1234567890", "logisticID": "SIGNCO202609110001",
        }
        with patch.object(settings, "db_mock_mode", False), \
             patch.object(settings, "db_live_order_enabled", True), \
             patch.object(client, "_request", return_value=response):
            result = client.create_order({**ORDER, "express_type": "XJBK"})
        self.assertTrue(result["success"])
        self.assertEqual(result["courier_order_no"], "DPK1234567890")

    def test_price_query_uses_official_address_and_measurement_fields(self):
        client = self.configured_client()
        payload = client._build_price_query_params({**ORDER, "item_volume": 0.006})
        self.assertEqual(payload["logisticCompanyID"], "DEPPON")
        self.assertEqual(payload["originalsaddress"], "福建省-厦门市-思明区,示例路1号")
        self.assertEqual(payload["originalsStreet"], "福建省-厦门市-湖里区,测试路2号")
        self.assertEqual(payload["totalWeight"], 1.2)
        self.assertEqual(payload["totalVolume"], 0.006)
        self.assertNotIn("customerCode", payload)

    def test_price_query_uses_official_default_when_volume_is_omitted(self):
        client = self.configured_client()
        payload = client._build_price_query_params(ORDER)
        self.assertEqual(payload["totalVolume"], 0.001)

    def test_price_query_uses_future_pickup_appointment_for_eta(self):
        client = self.configured_client()
        payload = client._build_price_query_params({
            **ORDER, "pickup_start_time": "2099-12-31T08:00",
        })
        self.assertEqual(payload["sendDateTime"], "2099-12-31 08:00:00")

    def test_price_query_maps_all_returned_products_to_page_cards(self):
        client = self.configured_client()
        response = {"result": "true", "resultCode": "1000", "responseParam": [
            {"producteCode": "TZKJC", "productName": "特快专递", "totalfee": 18, "days": "明日达"},
            {"producteCode": "XJBK", "productName": "小件标快", "totalfee": 12, "arriveDate": "2026-09-17 18:00"},
            {"producteCode": "DJBK", "productName": "大件标快", "totalfee": 38, "days": "后日达"},
            {"producteCode": "HKDJC", "productName": "航空大件次日达", "totalfee": 68, "days": "次日达"},
        ]}
        with patch.object(client, "_request", return_value=response) as request:
            result = client.query_delivery_quotes({**ORDER, "item_volume": 0.006})
        self.assertTrue(result["success"])
        rows = {item["business_type"]: item for item in result["items"]}
        self.assertEqual(set(rows), {"XJTK", "XJBK", "DJTK", "DJBK"})
        self.assertEqual(rows["XJTK"]["estimated_fee"], 18.0)
        self.assertEqual(rows["XJBK"]["estimated_delivery"], "2026-09-17 18:00:00")
        self.assertEqual(
            [call.args[1]["productCode"] for call in request.call_args_list],
            ["TZKJC", "XJBK", "HKDJC", "DJBK"],
        )

    def test_quote_requests_all_four_products_separately(self):
        client = self.configured_client()
        responses = {
            "TZKJC": {"productName": "特快专递", "totalfee": 17, "arriveDate": "2026-09-20 09:15"},
            "XJBK": {"productName": "小件标快", "totalfee": 14, "arriveDate": "2026-09-21 09:15"},
            "HKDJC": {"productName": "特快次日", "totalfee": 180, "arriveDate": "2026-09-19 20:00"},
            "DJBK": {"productName": "大件标快", "totalfee": 80, "arriveDate": "2026-09-21 09:15"},
        }

        def fake_request(_url, payload):
            code = payload["productCode"]
            row = {"producteCode": code, **responses[code]}
            return {"result": "true", "resultCode": "1000", "responseParam": [row]}

        with patch.object(client, "_request", side_effect=fake_request) as request:
            result = client.query_delivery_quotes({**ORDER, "item_weight": 15})
        self.assertTrue(result["success"])
        rows = {item["business_type"]: item for item in result["items"]}
        self.assertEqual(set(rows), {"XJTK", "XJBK", "DJTK", "DJBK"})
        self.assertEqual(rows["XJTK"]["estimated_fee"], 17.0)
        self.assertEqual(rows["XJBK"]["estimated_fee"], 14.0)
        self.assertEqual(rows["DJTK"]["estimated_fee"], 180.0)
        self.assertEqual(rows["DJBK"]["estimated_fee"], 80.0)
        self.assertEqual(
            [call.args[1]["productCode"] for call in request.call_args_list],
            ["TZKJC", "XJBK", "HKDJC", "DJBK"],
        )

    def test_large_volume_still_queries_all_products_for_authoritative_availability(self):
        client = self.configured_client()

        def fake_request(_url, payload):
            code = payload["productCode"]
            return {"result": "true", "resultCode": "1000", "responseParam": [{
                "producteCode": code, "productName": code,
                "totalfee": 30, "arriveDate": "2026-09-20 09:15",
            }]}

        with patch.object(client, "_request", side_effect=fake_request) as request:
            result = client.query_delivery_quotes({
                **ORDER, "item_weight": 2, "item_volume": 0.12,
            })
        self.assertTrue(result["success"])
        self.assertEqual(
            [call.args[1]["productCode"] for call in request.call_args_list],
            ["TZKJC", "XJBK", "HKDJC", "DJBK"],
        )

    def test_zero_fee_is_not_presented_as_free_shipping(self):
        client = self.configured_client()
        response = {"result": "true", "resultCode": "1000", "responseParam": [{
            "producteCode": "HKDJC", "productName": "特快次日",
            "totalFee": 0, "arriveDate": "2026-09-19 20:00",
        }]}
        with patch.object(client, "_request", return_value=response):
            result = client.query_delivery_quotes({**ORDER, "item_weight": 15})
        rows = {item["business_type"]: item for item in result["items"]}
        self.assertIsNone(rows["DJTK"]["estimated_fee"])

    def test_zero_fee_large_express_falls_back_to_alternate_product(self):
        client = self.configured_client()

        def fake_request(_url, payload):
            code = payload["productCode"]
            fees = {"TZKJC": 17, "XJBK": 14, "HKDJC": 0, "HKDJG": 58, "DJBK": 36}
            return {"result": "true", "resultCode": "1000", "responseParam": [{
                "producteCode": code, "productName": "大件特快" if code.startswith("HK") else code,
                "totalfee": fees[code], "arriveDate": "2026-09-25 18:00",
            }]}

        with patch.object(client, "_request", side_effect=fake_request) as request:
            result = client.query_delivery_quotes({**ORDER, "item_weight": 15})
        rows = {item["business_type"]: item for item in result["items"]}
        self.assertEqual(rows["DJTK"]["estimated_fee"], 58.0)
        self.assertEqual(rows["DJTK"]["source_product_code"], "HKDJG")
        self.assertEqual(
            [call.args[1]["productCode"] for call in request.call_args_list],
            ["TZKJC", "XJBK", "HKDJC", "HKDJG", "DJBK"],
        )

    def test_relative_delivery_time_is_normalized_when_absolute_eta_is_absent(self):
        client = self.configured_client()
        self.assertEqual(
            client._normalize_delivery_time("后天9:15前", dt.datetime(2026, 9, 18, 8, 0)),
            "2026-09-20 09:15:00",
        )

    def test_query_order_uses_official_waybill_number_field(self):
        client = self.configured_client()
        with patch.object(settings, "db_mock_mode", False), \
             patch.object(client, "_request", return_value={"result": "true"}) as request:
            client.query_order(courier_order_no="DPK123")
        self.assertEqual(request.call_args.args[1]["waybillNumber"], "DPK123")
        self.assertNotIn("waybillNuber", request.call_args.args[1])

    def test_cancel_prefers_original_logistic_id_when_order_number_is_available(self):
        client = self.configured_client()
        with patch.object(settings, "db_mock_mode", False), \
             patch.object(settings, "db_live_order_enabled", True), \
             patch.object(client, "_request", return_value={"result": "true", "resultCode": "1000"}) as request:
            result = client.cancel_order("DPK123", ORDER["order_no"])
        self.assertTrue(result["success"])
        payload = request.call_args.args[1]
        self.assertEqual(payload["logisticID"], client._logistic_id(ORDER["order_no"]))
        self.assertNotIn("mailNo", payload)

    def test_cancel_is_idempotent_when_deppon_reports_already_cancelled(self):
        client = self.configured_client()
        response = {"result": "false", "resultCode": "2001", "reason": "订单状态为已撤销,不允许撤销操作"}
        with patch.object(settings, "db_mock_mode", False), \
             patch.object(settings, "db_live_order_enabled", True), \
             patch.object(client, "_request", return_value=response):
            result = client.cancel_order("DPK123", ORDER["order_no"])
        self.assertTrue(result["success"])
        self.assertEqual(result["message"], "德邦订单已撤销")

    def test_waybill_fee_query_uses_mailno_and_monthly_customer(self):
        client = self.configured_client()
        response = {"result": "true", "resultCode": "1000", "responseParam": {
            "mailNo": "DPK123", "transportPrice": 20, "insurance": 1, "totalPrice": 21,
        }}
        with patch.object(settings, "db_mock_mode", False), \
             patch.object(client, "_request", return_value=response) as request:
            result = client.query_actual_freight("DPK123")
        self.assertEqual(result["freight"], 21.0)
        self.assertEqual(request.call_args.args, (client.waybill_fee_url, {
            "mailNo": "DPK123",
            "customerCode": "MONTHLY",
        }))

    def test_request_preview_signs_without_exposing_appkey(self):
        client = self.configured_client()
        preview = client.build_gateway_preview("https://example.test", {"mailNo": "DPK123"})
        self.assertEqual(set(preview["form"]), {"companyCode", "digest", "timestamp", "params"})
        self.assertNotIn("APPKEY", json.dumps(preview))


class CourierCatalogTests(unittest.TestCase):
    def test_volume_weight_is_still_available_for_fee_estimates(self):
        from courier_services import estimate_chargeable_weight

        chargeable, volumetric = estimate_chargeable_weight(5, 0.12)
        self.assertAlmostEqual(volumetric, 20.0)
        self.assertAlmostEqual(chargeable, 20.0)

    def test_actual_weight_is_used_when_it_is_greater(self):
        from courier_services import estimate_chargeable_weight

        chargeable, volumetric = estimate_chargeable_weight(18, 0.01)
        self.assertGreater(chargeable, volumetric)
        self.assertEqual(chargeable, 18.0)

    def test_unavailable_discount_products_are_not_in_catalog(self):
        from courier_services import DB_SERVICES

        products = {service["express_type"] for service in DB_SERVICES}
        self.assertEqual(products, {"XJTK", "DJTK", "XJBK", "DJBK"})


class PickupWindowTests(unittest.TestCase):
    def test_expired_window_moves_to_nearest_slot_today(self):
        from api.order_dispatch import refresh_expired_pickup_window

        order = SimpleNamespace(
            pickup_time_mode="appointment",
            pickup_start_time=dt.datetime(2026, 9, 14, 14, 0),
            pickup_end_time=dt.datetime(2026, 9, 14, 16, 0),
        )
        changed = refresh_expired_pickup_window(
            order, now=dt.datetime(2026, 9, 14, 16, 30)
        )
        self.assertTrue(changed)
        self.assertEqual(order.pickup_start_time, dt.datetime(2026, 9, 14, 18, 0))
        self.assertEqual(order.pickup_end_time, dt.datetime(2026, 9, 14, 20, 0))

    def test_expired_evening_window_moves_to_next_morning(self):
        from api.order_dispatch import refresh_expired_pickup_window

        order = SimpleNamespace(
            pickup_time_mode="appointment",
            pickup_start_time=dt.datetime(2026, 9, 14, 18, 0),
            pickup_end_time=dt.datetime(2026, 9, 14, 20, 0),
        )
        refresh_expired_pickup_window(order, now=dt.datetime(2026, 9, 14, 18, 30))
        self.assertEqual(order.pickup_start_time, dt.datetime(2026, 9, 15, 8, 0))
        self.assertEqual(order.pickup_end_time, dt.datetime(2026, 9, 15, 10, 0))

    def test_future_window_is_kept(self):
        from api.order_dispatch import refresh_expired_pickup_window

        order = SimpleNamespace(
            pickup_time_mode="appointment",
            pickup_start_time=dt.datetime(2026, 9, 15, 10, 0),
            pickup_end_time=dt.datetime(2026, 9, 15, 12, 0),
        )
        changed = refresh_expired_pickup_window(
            order, now=dt.datetime(2026, 9, 14, 16, 30)
        )
        self.assertFalse(changed)
        self.assertEqual(order.pickup_start_time, dt.datetime(2026, 9, 15, 10, 0))


class RouteRegistrationTests(unittest.TestCase):
    def test_cancel_and_track_are_different_handlers(self):
        from api.routes.orders import router

        routes = {
            (tuple(sorted(route.methods or [])), route.path): route.endpoint.__name__
            for route in router.routes if hasattr(route, "methods")
        }
        self.assertEqual(routes[(('POST',), '/{order_id}/cancel')], "cancel_order")
        self.assertEqual(routes[(('GET',), '/{order_id}/track')], "get_track")


class AddressValidationTests(unittest.TestCase):
    def test_standard_division_and_municipality(self):
        from api.address_validation import validate_division

        self.assertEqual(validate_division("福建省", "厦门市", "思明区"), "")
        self.assertEqual(validate_division("北京市", "北京市", "朝阳区"), "")
        self.assertIn("不属于", validate_division("福建省", "厦门市", "天河区"))


class AddressParserTests(unittest.TestCase):
    def test_extracts_full_divisions_with_spaces(self):
        from utils.address_parser import parse_recipient

        result = parse_recipient(
            "演示员工 13800000088 四川省眉山市彭山区示例路1号"
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["name"], "演示员工")
        self.assertEqual(result["phone"], "13800000088")
        self.assertEqual(result["province"], "四川省")
        self.assertEqual(result["city"], "眉山市")
        self.assertEqual(result["district"], "彭山区")
        self.assertEqual(result["address"], "示例路1号")

    def test_extracts_full_divisions_with_commas(self):
        from utils.address_parser import parse_recipient

        result = parse_recipient(
            "演示员工，13800000088，四川省，眉山市，彭山区，示例路1号"
        )
        self.assertEqual(
            (result["province"], result["city"], result["district"]),
            ("四川省", "眉山市", "彭山区"),
        )
        self.assertEqual(result["address"], "示例路1号")

    def test_accepts_short_province_name(self):
        from utils.address_parser import parse_recipient

        result = parse_recipient(
            "演示员工, 13800000088, 四川 眉山市 彭山区 示例路1号"
        )
        self.assertEqual(
            (result["province"], result["city"], result["district"]),
            ("四川省", "眉山市", "彭山区"),
        )

    def test_phone_at_end_does_not_turn_address_into_name(self):
        from utils.address_parser import parse_recipient

        result = parse_recipient(
            "演示员工 四川省成都市示例区示例路1号 13800000088"
        )
        self.assertEqual(result["name"], "演示员工")
        self.assertEqual(result["phone"], "13800000088")
        self.assertEqual(
            (result["province"], result["city"], result["district"]),
            ("四川省", "成都市", ""),
        )
        self.assertEqual(result["address"], "示例区示例路1号")


class RuntimeCourierConfigurationTests(unittest.TestCase):
    def test_deppon_uses_values_saved_in_system_config(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from courier import get_courier
        from database.connection import Base
        from database import models

        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            values = {
                "db_company_code": "COMPANY-DB",
                "db_customer_code": "MONTHLY-DB",
                "db_appkey": "SECRET-DB",
                "db_sign": "SIGN-DB",
            }
            for key, value in values.items():
                db.add(models.SystemConfig(config_key=key, config_value=value))
            db.commit()
            client = get_courier("DB", db)
            self.assertEqual(client.company_code, "COMPANY-DB")
            self.assertEqual(client.customer_code, "MONTHLY-DB")
            self.assertEqual(client.appkey, "SECRET-DB")
            self.assertEqual(client.sign_value, "SIGN-DB")
        finally:
            db.close()
            engine.dispose()

    def test_jdl_uses_values_saved_in_system_config(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from courier import get_courier
        from database.connection import Base
        from database import models

        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            values = {
                "jdl_app_key": "APP-KEY",
                "jdl_app_secret": "APP-SECRET",
                "jdl_access_token": "ACCESS-TOKEN",
                "jdl_customer_code": "MONTHLY-CODE",
                "jdl_business_unit_code": "BUSINESS-UNIT",
            }
            for key, value in values.items():
                db.add(models.SystemConfig(config_key=key, config_value=value))
            db.commit()
            client = get_courier("JDL", db)
            self.assertEqual(client.app_key, "APP-KEY")
            self.assertEqual(client.app_secret, "APP-SECRET")
            self.assertEqual(client.access_token, "ACCESS-TOKEN")
            self.assertEqual(client.customer_code, "MONTHLY-CODE")
            self.assertEqual(client.business_unit_code, "BUSINESS-UNIT")
        finally:
            db.close()
            engine.dispose()


class JDLCourierTests(unittest.TestCase):
    def test_waybill_extraction_accepts_scalar_and_string_list_responses(self):
        from courier.jdl import JDLCourier

        keys = ("waybillCode", "waybillCodes", "deliveryId")
        self.assertEqual(JDLCourier._find_first("JD0001", keys), "JD0001")
        self.assertEqual(JDLCourier._find_first(["JD0002"], keys), "JD0002")
        self.assertEqual(
            JDLCourier._find_first({"data": ["JD0003"]}, keys), "JD0003"
        )
        self.assertEqual(
            JDLCourier._find_first({"message": "成功", "data": {}}, keys), ""
        )

    def test_nested_business_failure_exposes_specific_reason_not_generic_gateway_text(self):
        from courier.jdl import JDLCourier

        response = {
            "success": False,
            "code": "500",
            "message": "京东物流业务处理失败",
            "data": {
                "resultCode": "1206002",
                "errorMessage": "订单不存在：请核对商家订单号和月结账号",
            },
        }
        ok, message, code, _data = JDLCourier._business_result(response)
        self.assertFalse(ok)
        self.assertEqual(code, "1206002")
        self.assertEqual(message, "订单不存在：请核对商家订单号和月结账号")

    def test_precheck_payload_maps_products_and_addresses(self):
        from courier.jdl import JDLCourier

        client = JDLCourier("app", "secret", "token", "monthly", "business-unit")
        order = {
            "sender_name": "寄件人", "sender_phone": "13800000000",
            "sender_company": "寄件公司",
            "sender_province": "福建省", "sender_city": "厦门市",
            "sender_district": "思明区", "sender_address": "测试地址1号",
            "receiver_name": "收件人", "receiver_phone": "13900000000",
            "receiver_company": "收件公司",
            "receiver_province": "福建省", "receiver_city": "厦门市",
            "receiver_district": "湖里区", "receiver_address": "测试地址2号",
            "item_name": "文件", "item_weight": 1, "parcel_count": 3,
            "item_count": 9, "insurance_amount": 100, "item_remark": "易碎",
        }
        express = client._build_precheck_payload(order, "ed-m-0001")
        freight = client._build_precheck_payload(order, "fr-m-0004")
        bulky = client._build_precheck_payload(order, "fr-m-0007")
        self.assertEqual(express["orderOrigin"], 1)
        self.assertEqual(freight["orderOrigin"], 4)
        self.assertEqual(bulky["orderOrigin"], 4)
        self.assertEqual(freight["businessUnitCode"], "business-unit")
        self.assertEqual(bulky["businessUnitCode"], "business-unit")
        self.assertNotIn("customerCode", freight)
        self.assertNotIn("goods", freight)
        self.assertEqual(express["productsReq"]["productCode"], "ed-m-0001")
        self.assertEqual(express["cargoes"][0]["quantity"], 3)
        self.assertEqual(express["cargoes"][0]["remark"], "易碎")
        self.assertEqual(express["senderContact"]["company"], "寄件公司")
        self.assertEqual(express["receiverContact"]["company"], "收件公司")
        self.assertEqual(express["productsReq"]["addedProducts"], [{
            "productCode": "ed-a-0002",
            "productAttrs": {"guaranteeMoney": "100"},
        }])
        self.assertEqual(freight["productsReq"]["addedProducts"][0]["productCode"], "fr-a-0002")
        self.assertEqual(express["CommonChannelInfo"]["channelCode"], "0030001")
        self.assertIn("福建省厦门市思明区", express["senderContact"]["fullAddress"])

    def test_create_payload_maps_pickup_window_to_epoch_millis(self):
        from courier.jdl import JDLCourier

        client = JDLCourier("app", "secret", "token", "monthly")
        payload = client._build_create_order_payload({
            "order_no": "ORDER-1", "express_type": "ed-m-0001",
            "sender_name": "寄件人", "sender_phone": "13800000000",
            "sender_province": "四川省", "sender_city": "成都市",
            "sender_district": "成华区", "sender_address": "测试地址1号",
            "receiver_name": "收件人", "receiver_phone": "13900000000",
            "receiver_province": "四川省", "receiver_city": "成都市",
            "receiver_district": "成华区", "receiver_address": "测试地址2号",
            "item_name": "文件", "item_weight": 1, "parcel_count": 2,
            "pickup_start_time": "2026-10-22 10:00:00",
            "pickup_end_time": "2026-10-22 12:00:00",
        }, "ed-m-0001")
        self.assertEqual(payload["cargoes"][0]["quantity"], 2)
        self.assertIsInstance(payload["pickupStartTime"], int)
        self.assertIsInstance(payload["pickupEndTime"], int)
        self.assertEqual(payload["pickupEndTime"] - payload["pickupStartTime"], 7200000)

    def test_sign_uses_exact_compact_json(self):
        from courier.jdl import JDLCourier

        client = JDLCourier("app", "secret", "token", "monthly")
        body = '[{"customerCode":"monthly"}]'
        timestamp = "2026-10-16 12:00:00"
        expected = hashlib.md5(
            ("secretaccess_tokentokenapp_keyappmethod/ecap/v1/orders/precheck"
             f"param_json{body}timestamp{timestamp}v2.0secret").encode("utf-8")
        ).hexdigest()
        self.assertEqual(client._sign("/ecap/v1/orders/precheck", body, timestamp), expected)

    def test_create_order_normalizes_nested_waybill_for_system_dispatch(self):
        from courier.jdl import JDLCourier

        client = JDLCourier("app", "secret", "token", "monthly")
        order = {
            "order_no": "ORDER-1", "express_type": "ed-m-0001",
            "sender_name": "寄件人", "sender_phone": "13800000000",
            "sender_province": "四川省", "sender_city": "成都市",
            "sender_district": "成华区", "sender_address": "示例路24号",
            "receiver_name": "收件人", "receiver_phone": "13900000000",
            "receiver_province": "四川省", "receiver_city": "成都市",
            "receiver_district": "成华区", "receiver_address": "示例路24号",
            "item_name": "文件", "item_weight": 1,
        }
        response = {"success": True, "code": 0, "data": {"result": {"waybillCode": "JD0001"}}}
        with patch.object(settings, "jdl_live_order_enabled", True), \
             patch.object(client, "_request", return_value=response):
            result = client.create_order(order)
        self.assertTrue(result["success"])
        self.assertEqual(result["courier_order_no"], "JD0001")
        self.assertEqual(result["courier_order_nos"], ["JD0001"])

    def test_create_order_auto_pregets_waybill_and_sends_it_to_create(self):
        from courier.jdl import JDLCourier

        client = JDLCourier("app", "secret", "token", "monthly")
        order = {
            "order_no": "ORDER-PREGET", "express_type": "ed-m-0001",
            "sender_name": "寄件人", "sender_phone": "13800000000",
            "sender_province": "四川省", "sender_city": "成都市",
            "sender_district": "成华区", "sender_address": "测试地址1号",
            "receiver_name": "收件人", "receiver_phone": "13900000000",
            "receiver_province": "四川省", "receiver_city": "成都市",
            "receiver_district": "成华区", "receiver_address": "测试地址2号",
            "item_name": "文件", "item_weight": 1,
        }
        responses = [
            {"success": True, "code": 0, "data": ["JD-PREGET-001"]},
            {"success": True, "code": 0, "data": {}},
        ]
        with patch.object(settings, "jdl_live_order_enabled", True), \
             patch.object(client, "_request", side_effect=responses) as request:
            result = client.create_order(order)
        self.assertTrue(result["success"])
        self.assertTrue(result["waybill_pregeted"])
        self.assertEqual(result["courier_order_no"], "JD-PREGET-001")
        self.assertEqual(request.call_args_list[0].args[0], "/ecap/v1/orders/pregetwaybillcodes")
        self.assertEqual(request.call_args_list[1].args[0], "/ecap/v1/orders/create")
        self.assertEqual(request.call_args_list[1].args[1]["waybillCode"], "JD-PREGET-001")

    def test_create_order_does_not_call_create_when_waybill_preget_fails(self):
        from courier.jdl import JDLCourier

        client = JDLCourier("app", "secret", "token", "monthly")
        with patch.object(settings, "jdl_live_order_enabled", True), \
             patch.object(client, "_request", return_value={
                 "success": False, "code": "403", "message": "无预取权限",
             }) as request:
            result = client.create_order({"order_no": "ORDER-1", "express_type": "ed-m-0001"})
        self.assertFalse(result["success"])
        self.assertIn("预取失败", result["message"])
        self.assertEqual(request.call_count, 1)

    def test_create_timeout_recovers_by_querying_preget_waybill(self):
        from courier.jdl import JDLCourier

        client = JDLCourier("app", "secret", "token", "monthly")
        order = {
            "order_no": "ORDER-RECOVER", "express_type": "ed-m-0001",
            "waybill_code": "JD-RECOVER-001",
            "sender_name": "寄件人", "sender_phone": "13800000000",
            "sender_province": "四川省", "sender_city": "成都市",
            "sender_district": "成华区", "sender_address": "测试地址1号",
            "receiver_name": "收件人", "receiver_phone": "13900000000",
            "receiver_province": "四川省", "receiver_city": "成都市",
            "receiver_district": "成华区", "receiver_address": "测试地址2号",
            "item_name": "文件", "item_weight": 1,
        }
        responses = [
            {"_transport_error": "京东物流网络错误：Timeout"},
            {"success": True, "code": 0, "message": "成功", "data": {"status": "已接单"}},
        ]
        with patch.object(settings, "jdl_live_order_enabled", True), \
             patch.object(client, "_request", side_effect=responses) as request:
            result = client.create_order(order)
        self.assertTrue(result["success"])
        self.assertTrue(result["recovery_used"])
        self.assertEqual(result["courier_order_no"], "JD-RECOVER-001")
        self.assertEqual(request.call_args_list[1].args[0], "/ecap/v1/orders/status/get")
        self.assertEqual(request.call_args_list[1].args[1]["orderCode"], "")

    def test_cancel_retries_with_waybill_only_when_local_order_code_is_unknown(self):
        from courier.jdl import JDLCourier

        client = JDLCourier("app", "secret", "token", "monthly")
        responses = [
            {"success": False, "code": "1206002", "message": "订单不存在"},
            {"success": True, "code": 0, "message": "成功", "data": {}},
        ]
        with patch.object(settings, "jdl_live_order_enabled", True), \
             patch.object(client, "_request", side_effect=responses) as request:
            result = client.cancel_order("JD0001", "LOCAL-ORDER-1")
        self.assertTrue(result["success"])
        self.assertTrue(result["fallback_used"])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].args[1]["orderCode"], "LOCAL-ORDER-1")
        self.assertEqual(request.call_args_list[1].args[1]["orderCode"], "")

    def test_status_query_retries_with_waybill_only_for_unknown_local_order_code(self):
        from courier.jdl import JDLCourier

        client = JDLCourier("app", "secret", "token", "monthly")
        responses = [
            {"success": False, "code": "1206002", "message": "订单不存在"},
            {"success": True, "code": 0, "message": "成功", "data": {"status": "已接单"}},
        ]
        with patch.object(client, "_request", side_effect=responses) as request:
            result = client.query_order("LOCAL-ORDER-1", "JD0001")
        self.assertTrue(result["success"])
        self.assertTrue(result["fallback_used"])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[1].args[1]["orderCode"], "")

    def test_actual_freight_reads_order_fee_details_not_preorder_estimate(self):
        from courier.jdl import JDLCourier

        client = JDLCourier("app", "secret", "token", "monthly")
        response = {
            "success": True, "code": 0, "data": {
                "freightPre": 15,
                "commonFeeInfoResponse": {
                    "totalAmount": 18,
                    "feeDetailInfoResponseList": [
                        {"productCode": "ed-m-0001", "amount": 17},
                        {"productCode": "ed-a-0002", "amount": 1},
                    ],
                },
            },
        }
        with patch.object(client, "_request", return_value=response) as request:
            result = client.query_actual_freight("JD0001")
        self.assertTrue(result["success"])
        self.assertEqual(result["freight"], 18.0)
        self.assertEqual(len(result["fee_items"]), 2)
        self.assertEqual(request.call_args.args[0], "/ecap/v1/orders/actualfee/query")
        self.assertEqual(request.call_args.args[1], {
            "waybillCode": "JD0001", "orderCode": "", "orderOrigin": 1,
            "customerCode": "monthly",
        })

    def test_actual_freight_sums_detail_when_total_is_missing(self):
        from courier.jdl import JDLCourier

        client = JDLCourier("app", "secret", "token", "monthly")
        with patch.object(client, "_request", return_value={
            "success": True, "code": 0,
            "data": {"result": {"commonFeeInfoResponse": {
                "feeDetailInfoResponseList": [{"amount": "12"}, {"amount": "1.5"}],
            }}},
        }):
            result = client.query_actual_freight("JD0002")
        self.assertEqual(result["freight"], 13.5)

    def test_actual_freight_accepts_direct_fee_response(self):
        from courier.jdl import JDLCourier

        client = JDLCourier("app", "secret", "token", "monthly")
        with patch.object(client, "_request", return_value={
            "success": True, "code": 0,
            "data": {"totalAmount": "21.80", "feeDetailInfoResponseList": []},
        }):
            result = client.query_actual_freight("JD0003")
        self.assertTrue(result["success"])
        self.assertEqual(result["freight"], 21.8)

    def test_actual_freight_reads_production_actual_fee_shape(self):
        from courier.jdl import JDLCourier

        client = JDLCourier("app", "secret", "token", "monthly")
        with patch.object(client, "_request", return_value={
            "success": True, "code": 0,
            "data": {
                "actualOrderWeight": 1.0,
                "sumMoney": 0,
                "totalAfterBeforehandDiscount": 16.0,
                "totalStandardAmount": 16.0,
                "commonActualFeeInfoDetails": [
                    {"feeType": "KDGFQFJF", "feeTypeName": "附加费", "money": 0.0,
                     "afterBeforehandDiscount": 0.0},
                    {"feeType": "QIPSF", "feeTypeName": "配送费", "money": 16.0,
                     "afterBeforehandDiscount": 16.0},
                ],
            },
        }):
            result = client.query_actual_freight("JDVD11090659044")
        self.assertTrue(result["success"])
        self.assertEqual(result["freight"], 16.0)
        self.assertEqual(result["fee_items"][1]["feeType"], "QIPSF")


class AdminSettingsPageTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from api.main import app
        from config.settings import settings
        from database.connection import Base, get_db

        self._login_enabled = settings.dingtalk_login_enabled
        settings.dingtalk_login_enabled = False
        self.app = app
        self.get_db = get_db
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

        self.app.dependency_overrides[self.get_db] = override_db
        self.client = TestClient(self.app)

    def tearDown(self):
        from config.settings import settings
        settings.dingtalk_login_enabled = self._login_enabled
        self.app.dependency_overrides.clear()
        self.engine.dispose()

    def test_enabled_carrier_secrets_are_visible_and_removed_sections_stay_hidden(self):
        from database import models

        db = self.session_factory()
        try:
            db.add_all([
                models.SystemConfig(config_key="sf_check_word", config_value="SF_SECRET_TEST"),
                models.SystemConfig(config_key="db_appkey", config_value="DB_SECRET_TEST"),
                models.SystemConfig(config_key="jdl_app_key", config_value="JDL_APP_KEY_TEST"),
                models.SystemConfig(config_key="jdl_app_secret", config_value="JDL_SECRET_TEST"),
                models.SystemConfig(config_key="jdl_access_token", config_value="JDL_TOKEN_TEST"),
                models.SystemConfig(config_key="jdl_customer_code", config_value="JDL_MONTHLY_TEST"),
                models.SystemConfig(config_key="jdl_business_unit_code", config_value="JDL_BU_TEST"),
                models.SystemConfig(
                    config_key="region.xiamen_other.enabled_couriers",
                    config_value='["SF","DB","JDL"]',
                ),
            ])
            db.commit()
        finally:
            db.close()

        response = self.client.get("/admin/settings")
        self.assertEqual(response.status_code, 200)
        self.assertIn('value="SF_SECRET_TEST"', response.text)
        self.assertIn('value="DB_SECRET_TEST"', response.text)
        self.assertIn('value="JDL_APP_KEY_TEST"', response.text)
        self.assertIn('value="JDL_SECRET_TEST"', response.text)
        self.assertIn('value="JDL_TOKEN_TEST"', response.text)
        self.assertIn('value="JDL_MONTHLY_TEST"', response.text)
        self.assertNotIn('value="JDL_BU_TEST"', response.text)
        self.assertIn('for="jdl_app_secret"', response.text)
        self.assertNotIn("京东快递快运”必须申请", response.text)
        self.assertNotIn("程序自动生成的公共参数", response.text)
        self.assertNotIn("公司与审批", response.text)

        empty_region = self.client.get("/admin/settings?region=beijing")
        self.assertNotIn('id="sf_customer_code"', empty_region.text)
        self.assertNotIn('id="db_company_code"', empty_region.text)
        self.assertNotIn('id="jdl_app_key"', empty_region.text)
        self.assertIn('<option value="JDL">京东物流</option>', empty_region.text)

    def test_saving_carrier_config_forces_approval_on(self):
        from database import models

        db = self.session_factory()
        try:
            db.add(models.SystemConfig(config_key="approval_required", config_value="0"))
            db.commit()
        finally:
            db.close()

    def test_saving_jdl_config_persists_all_required_values(self):
        from database import models

        values = {
            "jdl_app_key": "JDL-APP-KEY",
            "jdl_app_secret": "JDL-APP-SECRET",
            "jdl_access_token": "JDL-ACCESS-TOKEN",
            "jdl_customer_code": "JDL-MONTHLY-CODE",
            "jdl_business_unit_code": "JDL-BUSINESS-UNIT",
        }
        response = self.client.post("/api/admin/settings", json={
            **values, "region_code": "chengdu", "enabled_carriers": ["JDL"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()["saved"]), set(values))

        db = self.session_factory()
        try:
            stored = {
                row.config_key.removeprefix("region.chengdu."): row.config_value
                for row in db.query(models.SystemConfig).filter(
                    models.SystemConfig.config_key.in_(
                        [f"region.chengdu.{key}" for key in values]
                    )
                ).all()
            }
            self.assertEqual(stored, values)
        finally:
            db.close()

        page = self.client.get("/admin/settings?region=chengdu")
        for key, value in values.items():
            if key == "jdl_business_unit_code":
                continue
            self.assertIn(f'value="{value}"', page.text)
        self.assertNotIn('value="JDL-BUSINESS-UNIT"', page.text)

        status = self.client.get("/api/courier/status?region=chengdu").json()
        self.assertTrue(status["jdl"]["credentials_configured"])
        self.assertTrue(status["jdl"]["billing_configured"])
        self.assertTrue(status["jdl"]["freight_configured"])

        response = self.client.post(
            "/api/admin/settings", json={"sf_customer_code": "SF-CUSTOMER"}
        )
        self.assertEqual(response.status_code, 200)

        db = self.session_factory()
        try:
            approval = db.query(models.SystemConfig).filter(
                models.SystemConfig.config_key == "approval_required"
            ).one()
            self.assertEqual(approval.config_value, "1")
        finally:
            db.close()

    def test_admin_dashboard_has_shipping_summary_without_same_city_delivery(self):
        with patch.object(settings, "sf_city_feature_enabled", False), patch.object(
            settings, "sf_city_live_order_enabled", False
        ):
            response = self.client.get(
                "/admin?start_date=2026-09-11&end_date=2026-09-17"
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("订单统计", response.text)
        self.assertIn("快递单量", response.text)
        self.assertIn("本月快递包裹最新状态", response.text)
        self.assertIn("每单只计1次", response.text)
        for label in (
            "待审核", "审核未通过", "待揽收", "已揽收", "在途",
            "派送中", "已签收", "异常包裹", "已取消",
        ):
            self.assertIn(label, response.text)
        self.assertNotIn("同城急送", response.text)
        self.assertIn('href="/admin/addresses"', response.text)
        self.assertEqual(self.client.get("/admin/addresses").status_code, 200)

        approval_page = self.client.get("/admin/approvals")
        self.assertEqual(approval_page.status_code, 200)
        self.assertIn("快递包裹", approval_page.text)
        self.assertIn('/static/js/order-detail.js', approval_page.text)
        self.assertNotIn("申请单号", approval_page.text)
        self.assertNotIn("drawer-order-no", approval_page.text)
        orders_page = self.client.get("/orders")
        self.assertNotIn("drawer-order-no", orders_page.text)
        self.assertNotIn("{{ o.order_no }}", orders_page.text)
        self.assertIn('const scope="mine";', orders_page.text)
        self.assertNotIn("const scope=&#34;", orders_page.text)
        express_page = self.client.get("/express")
        self.assertNotIn('单号: " + j.order_no', express_page.text)

        self.assertIn("订单详情", approval_page.text)
        self.assertIn('href="/admin/approvals"', approval_page.text)
        self.assertIn('id="role-view"', approval_page.text)
        self.assertIn('data-roles="employee,admin"', approval_page.text)
        self.assertIn('const canApprove=()=>true', approval_page.text)
        self.assertNotIn('admin_approver', approval_page.text)

        landing_page = self.client.get('/')
        self.assertIn('data-roles="employee,admin" href="/admin/approvals"', landing_page.text)
        self.assertIn('data-roles="admin" href="/admin"', landing_page.text)
        self.assertNotIn('只读查看申请、审批记录与处理进度', landing_page.text)
        self.assertIn('href="/feedback"', landing_page.text)
        self.assertIn('href="/admin/feedback"', self.client.get("/admin").text)
        self.assertIn('href="/admin/orders?month=', response.text)
        self.assertIn('&include_cancelled=1"', response.text)
        self.assertIn('status=pending_pickup', response.text)
        self.assertIn('href="/admin/orders"', response.text)

    def test_order_status_is_aligned_across_list_detail_approval_and_api(self):
        import datetime as dt
        from database import models

        db = self.session_factory()
        try:
            user = models.User(dingtalk_userid="status-user", name="状态员工", role="user")
            db.add(user)
            db.flush()
            order = models.Order(
                order_no="STATUS-ALIGN-1", courier_type="DB",
                courier_order_no="DPK-STATUS-1", express_type_name="小件标快",
                sender_name="寄件人", sender_phone="13800000000",
                sender_address="测试路1号", receiver_name="收件人",
                receiver_phone="13900000000", receiver_province="福建省",
                receiver_city="厦门市", receiver_address="测试路2号",
                item_name="文件", status="ordered", track_status="已下单",
                created_by=user.id, created_at=dt.datetime(2026, 10, 13, 12, 0),
            )
            db.add(order)
            db.flush()
            db.add(models.CourierTrack(
                order_id=order.id, courier_order_no=order.courier_order_no,
                track_time=dt.datetime(2026, 10, 13, 12, 1),
                track_status="已下单", track_detail="等待快递员上门揽收",
            ))
            db.commit()
            order_id = order.id
        finally:
            db.close()

        page = self.client.get(
            "/admin/orders?start_date=2026-10-13&end_date=2026-10-13&include_cancelled=1"
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn("DPK-STATUS-1", page.text)
        self.assertIn(
            '<span class="state-pill state-pending_pickup">待揽收</span>',
            page.text,
        )
        self.assertNotIn(
            '<span class="state-pill state-pending_pickup">已下单</span>',
            page.text,
        )

        detail = self.client.get(f"/api/orders/{order_id}").json()["order"]
        self.assertEqual(detail["track_status"], "已下单")
        self.assertEqual(detail["status_category"], "pending_pickup")
        self.assertEqual(detail["status_label"], "待揽收")

        api_item = next(
            item for item in self.client.get("/api/orders").json()["items"]
            if item["id"] == order_id
        )
        self.assertEqual(api_item["status_category"], "pending_pickup")
        self.assertEqual(api_item["status_label"], "待揽收")

        snapshot = self.client.get(
            f"/api/orders/status-snapshot?order_ids={order_id}"
        ).json()
        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["items"][0]["status_category"], "pending_pickup")
        self.assertEqual(snapshot["items"][0]["status_label"], "待揽收")
        self.assertIn("function applyOrderRowStatus(order)", page.text)
        self.assertIn("setInterval(syncVisibleOrderStatuses,60000)", page.text)
        self.assertIn("applyOrderRowStatus(data.order); renderOrder", page.text)

        approval_page = self.client.get("/admin/approvals")
        self.assertEqual(approval_page.status_code, 200)
        self.assertIn("DPK-STATUS-1", approval_page.text)
        self.assertIn("待揽收", approval_page.text)

    def test_admin_order_page_lists_all_employees_and_applies_filters(self):
        import datetime as dt
        from database import models

        db = self.session_factory()
        try:
            first = models.User(dingtalk_userid="employee-a", name="员工甲", role="user")
            second = models.User(dingtalk_userid="employee-b", name="员工乙", role="user")
            db.add_all([first, second])
            db.flush()
            base = dict(
                courier_type="SF", sender_name="寄件人", sender_phone="13800000000",
                sender_address="软件园一期", receiver_phone="13900000000",
                receiver_province="福建省", receiver_city="厦门市",
                receiver_address="测试路1号", item_name="文件",
                created_at=dt.datetime(2026, 9, 22, 10, 30),
            )
            db.add_all([
                models.Order(
                    **base, order_no="ADMIN-ORDER-1", courier_order_no="SF001",
                    receiver_name="收件人甲", status="pending", created_by=first.id,
                ),
                models.Order(
                    **base, order_no="ADMIN-ORDER-2", courier_order_no="SF002",
                    receiver_name="收件人乙", status="success", track_status="在途",
                    created_by=second.id,
                ),
            ])
            db.commit()
        finally:
            db.close()

        page = self.client.get(
            "/admin/orders?start_date=2026-09-22&end_date=2026-09-22&include_cancelled=1"
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn("员工寄件记录", page.text)
        self.assertIn("员工甲", page.text)
        self.assertIn("员工乙", page.text)
        self.assertIn("SF001", page.text)
        self.assertIn("SF002", page.text)
        self.assertIn("查看详情", page.text)
        self.assertIn("导出顺丰详情", page.text)
        self.assertIn("导出德邦详情", page.text)
        self.assertIn("exportCarrierOrders('SF')", page.text)
        self.assertIn('const scope="all";', page.text)
        self.assertNotIn("const scope=&#34;", page.text)

        filtered = self.client.get(
            "/admin/orders?start_date=2026-09-22&end_date=2026-09-22"
            "&status=transit&employee=员工乙&include_cancelled=1"
        )
        self.assertEqual(filtered.status_code, 200)
        self.assertNotIn("SF001", filtered.text)
        self.assertIn("SF002", filtered.text)

    def test_admin_order_page_collapses_batch_until_formal_ordering_finishes(self):
        import datetime as dt
        from database import models

        db = self.session_factory()
        try:
            user = models.User(dingtalk_userid="batch-owner", name="批量员工", role="user")
            db.add(user)
            db.flush()
            batch = models.Batch(
                batch_no="ADMIN-BATCH-ONE", courier_type="SF", region_code="xiamen_other",
                sender_name="批量寄件人", sender_phone="13800000000", item_name="文件",
                total_count=2, status="pending_approval", created_by=user.id,
                created_at=dt.datetime(2026, 9, 24, 10, 0),
            )
            db.add(batch)
            db.flush()
            common = dict(
                courier_type="SF", batch_id=batch.id, sender_name="批量寄件人",
                sender_phone="13800000000", sender_address="软件园",
                receiver_phone="13900000000", receiver_province="福建省",
                receiver_city="厦门市", receiver_address="测试路", item_name="文件",
                status="pending", created_by=user.id,
                created_at=dt.datetime(2026, 9, 24, 10, 0),
            )
            first = models.Order(**common, order_no="BATCH-CHILD-1", receiver_name="收件人甲")
            second = models.Order(**common, order_no="BATCH-CHILD-2", receiver_name="收件人乙")
            db.add_all([first, second])
            db.commit()
            batch_id, first_id, second_id = batch.id, first.id, second.id
        finally:
            db.close()

        query = "/admin/orders?start_date=2026-09-24&end_date=2026-09-24&include_cancelled=1"
        pending_page = self.client.get(query)
        self.assertIn("ADMIN-BATCH-ONE", pending_page.text)
        self.assertIn("批量寄件 · 2 票", pending_page.text)
        self.assertNotIn("BATCH-CHILD-1", pending_page.text)
        self.assertNotIn("BATCH-CHILD-2", pending_page.text)
        self.assertIn("function adminSkipApproval(kind,id,level,event)", pending_page.text)
        self.assertIn("adminSkipControls", pending_page.text)

        db = self.session_factory()
        try:
            db.query(models.Batch).filter(models.Batch.id == batch_id).update({"status": "done"})
            db.query(models.Order).filter(models.Order.id.in_((first_id, second_id))).update(
                {"status": "ordered"}, synchronize_session=False
            )
            db.commit()
        finally:
            db.close()

        completed_page = self.client.get(query)
        self.assertNotIn("ADMIN-BATCH-ONE", completed_page.text)
        self.assertIn("BATCH-CHILD-1", completed_page.text)
        self.assertIn("BATCH-CHILD-2", completed_page.text)

    def test_admin_exports_sf_and_deppon_as_two_attribution_sheets(self):
        import datetime as dt
        from io import BytesIO
        from openpyxl import load_workbook
        from database import models

        db = self.session_factory()
        try:
            user = models.User(
                dingtalk_userid="export-user", name="导出员工", role="admin",
                department="行政管理部",
            )
            project = models.Project(
                project_code="PROJECT-1", project_name="年会活动",
                full_path="市场活动/年会活动", dept_name="市场部",
            )
            db.add_all([user, project])
            db.flush()
            common = dict(
                sender_name="寄件人", sender_phone="13800000000", sender_address="软件园",
                receiver_name="收件人", receiver_phone="13900000000",
                receiver_province="福建省", receiver_city="厦门市", receiver_address="测试路",
                item_name="文件", item_weight=1.5, parcel_count=1, status="success",
                created_by=user.id, created_at=dt.datetime(2026, 9, 23, 9, 0),
            )
            db.add_all([
                models.Order(
                    **common, order_no="EXPORT-SF-DEPT", courier_type="SF",
                    courier_order_no=None, cost_type="department", dept_name="财务部",
                    express_type_name="顺丰标快", actual_freight=12.5, freight=11.0,
                ),
                models.Order(
                    **common, order_no="EXPORT-SF-PROJECT", courier_type="SF",
                    courier_order_no="SF1001", cost_type="project", project_code="PROJECT-1",
                    project_name="年会活动", dept_name="品牌部", express_type_name="顺丰特快",
                ),
                models.Order(
                    **common, order_no="EXPORT-DB-DEPT", courier_type="DB",
                    courier_order_no="DPK1001", cost_type="department", dept_name="人事部",
                    express_type_name="标准快递",
                ),
            ])
            db.commit()
        finally:
            db.close()

        query = "start_date=2026-09-23&end_date=2026-09-23&include_cancelled=1"
        sf_response = self.client.get(f"/api/admin/orders/export?courier_type=SF&{query}")
        self.assertEqual(sf_response.status_code, 200)
        self.assertIn("spreadsheetml", sf_response.headers["content-type"])
        sf_book = load_workbook(BytesIO(sf_response.content), data_only=True)
        self.assertEqual(sf_book.sheetnames, ["全部区域顺丰-部门", "全部区域顺丰-项目"])
        department = sf_book["全部区域顺丰-部门"]
        project_sheet = sf_book["全部区域顺丰-项目"]
        self.assertEqual([department.cell(1, 1).value, department.cell(1, 2).value], ["运单号", "部门"])
        self.assertIsNone(department.cell(2, 1).value)
        self.assertEqual(department.cell(2, 2).value, "财务部")
        self.assertEqual(
            [project_sheet.cell(1, column).value for column in range(1, 4)],
            ["运单号", "项目", "部门"],
        )
        self.assertEqual(project_sheet.cell(2, 1).value, "SF1001")
        self.assertEqual(project_sheet.cell(2, 2).value, "年会活动")
        self.assertEqual(project_sheet.cell(2, 3).value, "品牌部")
        for sheet in (department, project_sheet):
            headers = [cell.value for cell in sheet[1]]
            self.assertNotIn("预估费用(元)", headers)
            self.assertNotIn("预计送达时间", headers)

        db_response = self.client.get(f"/api/admin/orders/export?courier_type=DB&{query}")
        db_book = load_workbook(BytesIO(db_response.content), data_only=True)
        self.assertEqual(db_book["全部区域德邦-部门"].cell(2, 1).value, "DPK1001")
        self.assertEqual(db_book["全部区域德邦-项目"].max_row, 1)

    def test_employee_feedback_can_be_submitted_and_resolved_by_admin(self):
        from database import models

        db = self.session_factory()
        try:
            user = models.User(
                dingtalk_userid="feedback-user",
                name="反馈员工",
                phone="13800000001",
                role="user",
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            user_id = user.id
        finally:
            db.close()

        employee_page = self.client.get(f"/feedback?user_id={user_id}")
        self.assertEqual(employee_page.status_code, 200)
        self.assertIn("意见建议", employee_page.text)

        created = self.client.post(
            "/api/feedback",
            json={"user_id": user_id, "content": "希望订单筛选更清晰"},
        )
        self.assertEqual(created.status_code, 200)
        feedback_id = created.json()["id"]

        own_page = self.client.get(f"/feedback?user_id={user_id}")
        self.assertIn("希望订单筛选更清晰", own_page.text)
        self.assertIn("待处理", own_page.text)

        admin_page = self.client.get("/admin/feedback")
        self.assertEqual(admin_page.status_code, 200)
        self.assertIn("反馈员工", admin_page.text)
        self.assertIn("希望订单筛选更清晰", admin_page.text)

        resolved = self.client.post(f"/api/admin/feedback/{feedback_id}/resolve")
        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(resolved.json()["status"], "resolved")
        own_page = self.client.get(f"/feedback?user_id={user_id}")
        self.assertIn("已处理", own_page.text)

    def test_rejected_employee_order_has_edit_and_resubmit_flow(self):
        from database import models

        db = self.session_factory()
        try:
            order = models.Order(
                order_no="CO-RESUBMIT-1", courier_type="SF", status="rejected",
                region_code="xiamen_other", express_type="2",
                express_type_name="顺丰标快", sender_name="寄件人",
                sender_phone="13800000000", sender_province="福建省",
                sender_city="厦门市", sender_district="思明区",
                sender_address="示例园区", receiver_name="收件人",
                receiver_phone="13900000000", receiver_province="福建省",
                receiver_city="厦门市", receiver_district="湖里区",
                receiver_address="测试路1号", item_name="文件", item_weight=1,
                item_count=1, parcel_count=1, cost_type="department",
                dept_name="测试部门", shipping_reason="资料寄送",
            )
            db.add(order)
            db.commit()
            db.refresh(order)
            order_id = order.id
        finally:
            db.close()

        orders_page = self.client.get("/orders")
        self.assertEqual(orders_page.status_code, 200)
        self.assertIn(f"/express?resubmit_order_id={order_id}", orders_page.text)

        edit_page = self.client.get(f"/express?resubmit_order_id={order_id}")
        self.assertEqual(edit_page.status_code, 200)
        self.assertIn("编辑并重新提交", edit_page.text)
        self.assertIn('value="CO-RESUBMIT-1"', edit_page.text)
        self.assertIn("重新提交审核", edit_page.text)

        db = self.session_factory()
        try:
            order = db.query(models.Order).filter(models.Order.id == order_id).one()
            order.status = "failed"
            order.courier_order_no = "SF-ALREADY-SHIPPED"
            db.commit()
        finally:
            db.close()
        blocked = self.client.get(f"/express?resubmit_order_id={order_id}")
        self.assertEqual(blocked.status_code, 400)

    def test_item_types_are_managed_and_shown_as_order_dropdown(self):
        admin_page = self.client.get("/admin/item-types")
        self.assertEqual(admin_page.status_code, 200)
        self.assertIn("物品类型及默认快递推荐设置", admin_page.text)
        self.assertIn("itemTypes =", admin_page.text)

        response = self.client.post(
            "/api/admin/item-types",
            json={"item_types": ["文件", "样品"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item_types"], ["文件", "样品"])

        express_page = self.client.get("/express")
        self.assertEqual(express_page.status_code, 200)
        self.assertIn('<select id="item_name" name="item_name" required data-searchable', express_page.text)
        self.assertIn('name="item_weight" type="number" step="1" min="1" value="1"', express_page.text)
        self.assertIn('name="item_volume"', express_page.text)
        self.assertIn('name="insurance_amount"', express_page.text)
        self.assertIn('name="parcel_count"', express_page.text)
        self.assertIn('class="order-summary-bar"', express_page.text)
        self.assertIn('id="summary-freight"', express_page.text)
        self.assertIn('id="summary-insurance"', express_page.text)
        self.assertIn('id="summary-total"', express_page.text)
        self.assertIn('function getSelectedQuote()', express_page.text)
        self.assertIn('if (!document.getElementById("insurance-enabled")?.checked) return 0;', express_page.text)
        self.assertIn('const returnedTotalRaw = selectedQuote?.estimated_total_fee;', express_page.text)
        self.assertIn('? returnedTotal.toFixed(2)', express_page.text)
        self.assertIn(': (freight != null && insurance != null ? (freight + insurance).toFixed(2) : "--");', express_page.text)
        self.assertIn('weight: assumedWeight ? 1 : enteredWeight', express_page.text)
        self.assertNotIn('id="multi-parcel-note"', express_page.text)
        self.assertIn('支持保价 · 支持多包裹', express_page.text)
        self.assertNotIn('成都区域当前月结账号已完成生产下单验证', express_page.text)
        service_rows = self.client.get("/api/services?region=chengdu").json()["items"]
        service_descriptions = " ".join(row["difference"] for row in service_rows)
        self.assertIn('适合日常文件、办公用品及普通中小件', service_descriptions)
        self.assertIn('适合紧急文件、电子产品', service_descriptions)
        self.assertIn('查看全部快递产品', express_page.text)
        self.assertIn('查看顺丰 API 请求（联调检查）', express_page.text)
        self.assertIn('api_debug', express_page.text)
        self.assertIn('<option value="文件">文件</option>', express_page.text)
        self.assertIn('<option value="样品">样品</option>', express_page.text)
        self.assertNotIn('<option value="办公用品">办公用品</option>', express_page.text)


class MockOrderFlowTests(unittest.TestCase):
    def test_create_approve_dispatch_and_cancel_flow(self):
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from api.main import app
        from database.connection import Base, get_db

        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(engine)
        test_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)

        def override_db():
            db = test_session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_db
        mock_mode = patch.object(settings, "db_mock_mode", True)
        live_mode = patch.object(settings, "db_live_order_enabled", False)
        auth_mode = patch.object(settings, "dingtalk_login_enabled", False)
        mock_mode.start()
        live_mode.start()
        auth_mode.start()
        try:
            client = TestClient(app)
            created = client.post("/api/orders", json={
                "courier_type": "DB",
                "sender_name": "发件人", "sender_phone": "13800000000",
                "sender_province": "福建省", "sender_city": "厦门市", "sender_district": "思明区",
                "sender_address": "测试路1号",
                "receiver_name": "收件人", "receiver_phone": "13900000000",
                "receiver_province": "福建省", "receiver_city": "厦门市", "receiver_district": "湖里区",
                "receiver_address": "测试路2号",
                "item_name": "文件", "item_weight": 1, "item_volume": 0.006, "item_count": 1,
                "estimated_freight": 18.5,
                "express_type": "XJTK", "express_type_name": "小件特快",
                "created_by": "",
            })
            self.assertEqual(created.status_code, 200)
            rejected_multi = client.post("/api/orders", json={
                "courier_type": "DB",
                "sender_name": "发件人", "sender_phone": "13800000000",
                "sender_province": "福建省", "sender_city": "厦门市", "sender_district": "思明区",
                "sender_address": "测试路1号", "receiver_name": "收件人", "receiver_phone": "13900000000",
                "receiver_province": "福建省", "receiver_city": "厦门市", "receiver_district": "湖里区",
                "receiver_address": "测试路2号", "item_name": "文件", "item_weight": 1,
                "parcel_count": 2, "express_type": "XJTK",
            })
            self.assertEqual(rejected_multi.status_code, 400)
            self.assertIn("只支持顺丰", rejected_multi.json()["detail"])
            order_id = created.json()["id"]
            detail = client.get(f"/api/orders/{order_id}")
            self.assertEqual(detail.status_code, 200)
            # 客户端传入的运费不可信，保存值必须来自后端重新取得的产品报价。
            self.assertEqual(detail.json()["order"]["freight"], 12.5)
            approved = client.post(f"/api/approval/{order_id}/approve", json={})
            self.assertEqual(approved.status_code, 200)
            self.assertTrue(approved.json()["courier_order_no"].startswith("DPKMOCK"))
            cancelled = client.post(f"/api/orders/{order_id}/cancel")
            self.assertEqual(cancelled.status_code, 200)
            self.assertTrue(cancelled.json()["ok"])
        finally:
            auth_mode.stop()
            live_mode.stop()
            mock_mode.stop()
            app.dependency_overrides.clear()
            engine.dispose()


class DingTalkClientTests(unittest.TestCase):
    def test_member_list_uses_supported_entry_order(self):
        from dingtalk.client import DingTalkClient

        client = DingTalkClient()
        with patch.object(client, "post", return_value={"result": {"list": []}}) as post:
            client.list_users_by_dept(123, recursive=False)

        payload = post.call_args.args[1]
        self.assertEqual(payload["dept_id"], 123)
        self.assertEqual(payload["order_field"], "entry_desc")
        self.assertFalse(payload["contain_access_users"])

    def test_member_list_fetches_every_page(self):
        from dingtalk.client import DingTalkClient

        client = DingTalkClient()
        pages = [
            {"errcode": 0, "result": {
                "list": [{"userid": "u1"}], "has_more": True, "next_cursor": 100,
            }},
            {"errcode": 0, "result": {
                "list": [{"userid": "u2"}], "has_more": False,
            }},
        ]
        with patch.object(client, "post", side_effect=pages) as post:
            users = client.list_users_by_dept(123, recursive=False)

        self.assertEqual([u["userid"] for u in users], ["u1", "u2"])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].args[1]["cursor"], 0)
        self.assertEqual(post.call_args_list[1].args[1]["cursor"], 100)

    def test_all_members_are_read_from_every_department_and_deduplicated(self):
        from dingtalk.client import DingTalkClient

        client = DingTalkClient()
        rows = {
            "1": [{"userid": "root", "dept_id_list": [1]}],
            "10": [
                {"userid": "u1", "name": "A", "dept_id_list": [10]},
                {"userid": "u2", "name": "B", "dept_id_list": [10]},
            ],
            "20": [
                {"userid": "u2", "name": "B", "dept_id_list": [20]},
                {"userid": "u3", "name": "C", "dept_id_list": [20]},
            ],
        }
        with patch.object(
            client, "list_users_by_dept",
            side_effect=lambda dept_id, recursive: rows[str(dept_id)],
        ) as loader:
            users = client.list_all_users(["10", "20"])
        self.assertEqual([item["userid"] for item in users], ["root", "u1", "u2", "u3"])
        self.assertEqual(set(next(item for item in users if item["userid"] == "u2")["dept_id_list"]), {"10", "20"})
        self.assertEqual(loader.call_count, 3)
        self.assertTrue(all(call.args[1] is False for call in loader.call_args_list))

if __name__ == "__main__":
    unittest.main()
