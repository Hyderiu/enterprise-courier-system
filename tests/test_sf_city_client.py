import base64
import hashlib
import json
import unittest
from unittest.mock import patch

from courier.sf_city import SFCityClient, SFCityCourier


class SFCityClientTests(unittest.TestCase):
    def test_sign_matches_documented_md5_hex_then_base64_rule(self):
        body = '{"company_id":9999999999004,"dev_id":9999999999002,"push_time":1}'
        expected = base64.b64encode(
            hashlib.md5(f"{body}&9999999999002&secret".encode()).hexdigest().encode()
        ).decode()
        self.assertEqual(SFCityClient.sign(body, "9999999999002", "secret"), expected)

    @patch("courier.sf_city.requests.post")
    @patch("courier.sf_city.time.time", return_value=1234567890)
    def test_company_create_signs_exact_body_sent(self, _time, post):
        post.return_value.raise_for_status.return_value = None
        post.return_value.json.return_value = {"error_code": 0, "result": {"sf_order_id": "1"}}
        client = SFCityClient("https://openic.sf-express.com/", "9999999999002", "secret")
        result = client.create_company_order({"company_id": 9999999999004})
        self.assertEqual(result["error_code"], 0)
        kwargs = post.call_args.kwargs
        body = kwargs["data"].decode("utf-8")
        self.assertEqual(json.loads(body)["push_time"], 1234567890)
        self.assertEqual(
            kwargs["params"]["sign"],
            SFCityClient.sign(body, "9999999999002", "secret"),
        )
        self.assertEqual(
            post.call_args.args[0],
            "https://openic.sf-express.com/open/api/external/createorder4c",
        )

    @patch("courier.sf_city.requests.post")
    def test_shop_create_uses_shop_endpoint(self, post):
        post.return_value.raise_for_status.return_value = None
        post.return_value.json.return_value = {"error_code": 0}
        client = SFCityClient("https://openic.sf-express.com", "9999999999002", "secret")
        client.create_shop_order({"shop_id": "9999999999003"})
        self.assertEqual(
            post.call_args.args[0],
            "https://openic.sf-express.com/open/api/external/createorder",
        )

    @patch("courier.sf_city.SFCityClient.pre_create_shop_order")
    def test_shop_precheck_converts_kg_to_grams_and_sends_coordinates(self, precreate):
        precreate.return_value = {
            "error_code": 0,
            "result": {"estimate_pay_money": 560, "promise_delivery_time": 30},
        }
        courier = SFCityCourier(
            "https://openic.sf-express.com", "1", "secret", "9999999999001"
        )
        result = courier.precheck_order({
            "receiver_province": "福建省", "receiver_city": "厦门市",
            "receiver_district": "思明区", "receiver_address": "示例园区",
            "receiver_lng": "118.10000", "receiver_lat": "24.490625",
            "item_weight": 1.2, "item_value": 1, "express_type": "10",
        })
        payload = precreate.call_args.args[0]
        self.assertEqual(payload["shop_id"], "9999999999001")
        self.assertEqual(payload["weight"], 1200)
        self.assertEqual(payload["total_price"], 1)
        self.assertEqual(payload["is_insured"], 0)
        self.assertNotIn("declared_value", payload)
        self.assertEqual(payload["user_lng"], "118.10000")
        self.assertEqual(payload["user_lat"], "24.490625")
        self.assertEqual(result["estimated_fee"], 5.6)
        self.assertEqual(result["promise_delivery_minutes"], 30)

    @patch("courier.sf_city.SFCityClient.create_shop_order")
    def test_shop_create_uses_fixed_shop_sender_and_receiver_payload(self, create):
        create.return_value = {
            "error_code": 0,
            "result": {
                "order_id": "123456", "total_price": 700,
                "real_pay_money": 560,
            },
        }
        courier = SFCityCourier(
            "https://openic.sf-express.com", "1", "secret", "9999999999001",
            live_order_enabled=True,
        )
        result = courier.create_order({
            "order_no": "CO-1", "receiver_name": "收件人",
            "sender_name": "申请人", "sender_phone": "13800000009",
            "receiver_phone": "13900000000", "receiver_province": "福建省",
            "receiver_city": "厦门市", "receiver_district": "思明区",
            "receiver_address": "示例园区", "receiver_lng": "118.1",
            "receiver_lat": "24.4", "item_name": "文件", "item_weight": 1,
            "item_value": 1, "item_count": 1, "express_type": "10",
            "item_remark": "到达后联系前台",
        })
        payload = create.call_args.args[0]
        self.assertNotIn("sender", payload)
        self.assertEqual(
            payload["remark"],
            "取件联系电话：13800000009\n\n到达后联系前台",
        )
        self.assertNotIn("product_remark", payload["order_detail"]["product_detail"][0])
        self.assertEqual(payload["order_detail"]["total_price"], 1)
        self.assertEqual(payload["receive"]["user_name"], "收件人")
        self.assertEqual(payload["order_detail"]["weight_gram"], 1000)
        self.assertTrue(result["success"])
        self.assertEqual(result["courier_order_no"], "123456")
        self.assertEqual(result["freight"], 5.6)

    @patch("courier.sf_city.SFCityClient.pre_create_shop_order")
    def test_shop_insurance_uses_declared_value_in_cents(self, precreate):
        precreate.return_value = {
            "error_code": 0, "result": {"estimate_pay_money": 560},
        }
        courier = SFCityCourier(
            "https://openic.sf-express.com", "1", "secret", "9999999999001"
        )
        courier.precheck_order({
            "receiver_city": "厦门市", "receiver_address": "示例园区",
            "receiver_lng": "118.1", "receiver_lat": "24.4",
            "item_weight": 1, "express_type": "10", "insurance_amount": 1234.56,
        })
        payload = precreate.call_args.args[0]
        self.assertEqual(payload["is_insured"], 1)
        self.assertEqual(payload["declared_value"], 123456)
        self.assertEqual(payload["total_price"], 123456)

    @patch("courier.sf_city.SFCityClient.create_shop_order")
    def test_shop_create_sends_declared_value_when_insured(self, create):
        create.return_value = {
            "error_code": 0, "result": {"order_id": "insured-order"},
        }
        courier = SFCityCourier(
            "https://openic.sf-express.com", "1", "secret", "9999999999001",
            live_order_enabled=True,
        )
        courier.create_order({
            "order_no": "CO-INSURED", "receiver_name": "收件人",
            "receiver_phone": "13900000000", "receiver_city": "厦门市",
            "receiver_address": "示例园区", "receiver_lng": "118.1",
            "receiver_lat": "24.4", "item_name": "证照", "item_weight": 1,
            "item_count": 1, "express_type": "36", "insurance_amount": 500,
        })
        payload = create.call_args.args[0]
        self.assertEqual(payload["is_insured"], 1)
        self.assertEqual(payload["declared_value"], 50000)
        self.assertEqual(payload["order_detail"]["total_price"], 50000)

    @patch("courier.sf_city.SFCityClient.list_order_feed")
    @patch("courier.sf_city.SFCityClient.get_order_status")
    def test_track_uses_realtime_status_and_feed_as_callback_fallback(self, status, feed):
        status.return_value = {"error_code": 0, "result": {
            "order_status": 10, "status_time": 123,
            "rider_name": "骑手甲", "rider_phone": "13811112222",
        }}
        feed.return_value = {"error_code": 0, "result": {"feed": [{
            "order_status": 1, "status_time": 100,
        }]}}
        courier = SFCityCourier(
            "https://openic.sf-express.com", "1", "secret", "9999999999001"
        )
        tracks = courier.query_track("SF-CITY-1")
        self.assertEqual(tracks[0]["status"], "骑手已接单")
        self.assertEqual(tracks[0]["courier"]["name"], "骑手甲")
        self.assertEqual(tracks[1]["status"], "寻找骑手中")

    @patch("courier.sf_city.SFCityClient.list_order_feed")
    @patch("courier.sf_city.SFCityClient.get_order_status")
    def test_track_does_not_treat_feed_generic_status_as_order_status(self, status, feed):
        status.return_value = {"error_code": 0, "result": {
            "order_status": 1, "status_time": 100,
        }}
        feed.return_value = {"error_code": 0, "result": {"feed": [{
            "status": 12, "status_desc": "配送员已到店", "status_time": 100,
        }]}}
        courier = SFCityCourier(
            "https://openic.sf-express.com", "1", "secret", "9999999999001"
        )
        tracks = courier.query_track("SF-CITY-1")
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["status"], "寻找骑手中")

    @patch("courier.sf_city.SFCityClient.get_order_status")
    def test_actual_freight_reads_confirmed_sf_city_amount(self, status):
        status.return_value = {"error_code": 0, "result": {
            "order_info": {"order_status": 17, "real_pay_money": 2350},
        }}
        courier = SFCityCourier(
            "https://openic.sf-express.com", "1", "secret", "9999999999001"
        )
        result = courier.query_actual_freight("SF-CITY-1")
        self.assertTrue(result["success"])
        self.assertEqual(result["freight"], 23.5)
        self.assertEqual(result["fee_detail"], {"deliveryFee": 23.5})

    @patch("courier.sf_city.SFCityClient.add_order_gratuity_fee")
    def test_add_gratuity_uses_cents_and_idempotency_serial(self, add_tip):
        add_tip.return_value = {"error_code": 0, "result": {
            "gratuity_fee": 500, "total_gratuity_fee": 800,
        }}
        courier = SFCityCourier(
            "https://openic.sf-express.com", "1", "secret", "9999999999001"
        )
        result = courier.add_gratuity("SF-CITY-1", 5, "TIP-SERIAL-1")
        add_tip.assert_called_once_with(
            "SF-CITY-1", "9999999999001", 500, "TIP-SERIAL-1"
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["gratuity_fee"], 5)
        self.assertEqual(result["total_gratuity_fee"], 8)


if __name__ == "__main__":
    unittest.main()
