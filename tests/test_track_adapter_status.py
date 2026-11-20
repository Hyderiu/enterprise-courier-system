import unittest
from unittest.mock import patch

from config.settings import settings
from courier.deppon import DepponCourier
from courier.sf import SFCourier
from courier.jdl import JDLCourier
from utils.order_status import classify_track_event


class CarrierTrackStatusTests(unittest.TestCase):
    def test_jdl_nested_trace_is_normalized_and_classified(self):
        client = JDLCourier("app", "secret", "token", "monthly")
        response = {"success": True, "code": 0, "data": {"result": {
            "traceDetails": [{
                "operationTime": "2026-10-21 09:30:00",
                "secondaryStatusName": "配送中",
                "operationDesc": "快递员正在派送",
                "siteName": "成都成华营业部",
            }]
        }}}
        with patch.object(client, "_request", return_value=response):
            tracks = client.query_track("JD0001")
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["time"], "2026-10-21 09:30:00")
        self.assertEqual(tracks[0]["status"], "配送中")
        self.assertIn("成都成华营业部", tracks[0]["detail"])
        self.assertEqual(classify_track_event(tracks[0]["status"], tracks[0]["detail"]), "delivering")

    def test_jdl_direct_trace_list_is_supported(self):
        client = JDLCourier("app", "secret", "token", "monthly")
        response = {"success": True, "code": 0, "data": [{
            "operateTime": "2026/10/21 10:00:00",
            "statusName": "配送完成",
            "traceMessage": "客户本人签收",
        }]}
        with patch.object(client, "_request", return_value=response):
            tracks = client.query_track("JD0002")
        self.assertEqual(tracks[0]["status"], "配送完成")
        self.assertEqual(classify_track_event(tracks[0]["status"], tracks[0]["detail"]), "delivered")

    def test_jdl_uses_official_standard_tracking_service(self):
        client = JDLCourier("app", "secret", "token", "monthly")
        response = {"code": 1000, "data": [{"trackings": [{
            "operationTime": "2026-10-21 10:00:00",
            "operationCode": "200072",
            "remark": "快件已揽收完成",
            "operateSite": "成都成华营业部",
        }]}]}
        with patch.object(client, "_request", return_value=response) as request:
            tracks = client.query_track("JD0003")
        request.assert_called_once_with(
            "/jd/tracking/query",
            {
                "referenceNumber": "JD0003", "referenceType": "20000",
                "customerCode": "monthly", "scope": 2,
            },
            lop_dn="Tracking_JD",
        )
        self.assertEqual(tracks[0]["status"], "已揽收")
        self.assertEqual(tracks[0]["op_code"], "200072")
        self.assertIn("快件已揽收完成", tracks[0]["detail"])
        self.assertEqual(tracks[0]["category"], "picked")

    def test_jdl_production_cancel_and_pickup_codes_use_canonical_statuses(self):
        client = JDLCourier("app", "secret", "token", "monthly")
        response = {"code": 1000, "data": [{"trackings": [{
            "operationTime": "2026-11-03 16:31:58",
            "operationCode": "200034",
            "remark": "您的快件将由快递员上门取件",
        }, {
            "operationTime": "2026-11-03 16:32:50",
            "operationCode": "200010",
            "remark": "寄件人已取消寄件",
        }]}]}
        with patch.object(client, "_request", return_value=response):
            tracks = client.query_track("JDVD11083343690")
        self.assertEqual(
            [(item["op_code"], item["status"], item["category"]) for item in tracks],
            [
                ("200034", "待揽收", "pending_pickup"),
                ("200010", "已取消", "cancelled"),
            ],
        )

    def test_jdl_falls_back_to_order_status_when_both_trace_services_are_empty(self):
        client = JDLCourier("app", "secret", "token", "monthly")
        responses = [
            {"success": True, "code": 0, "data": []},
            {"success": True, "code": 0, "data": []},
            {"success": True, "code": 0, "data": {
                "status": "690", "statusDesc": "已取消",
                "updateTime": "2026-11-03 16:32:50",
            }},
        ]
        with patch.object(client, "_request", side_effect=responses) as request:
            tracks = client.query_track("JDVD11083343690")
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["status"], "已取消")
        self.assertEqual(tracks[0]["category"], "cancelled")
        self.assertEqual(tracks[0]["op_code"], "690")
        self.assertEqual(tracks[0]["source"], "order_status")
        self.assertEqual(request.call_args_list[2].args[0], "/ecap/v1/orders/status/get")
        self.assertEqual(request.call_args_list[2].args[1]["orderCode"], "")

    def test_jdl_unknown_numeric_trace_is_supplemented_by_order_status(self):
        client = JDLCourier("app", "secret", "token", "monthly")
        responses = [
            {"code": 1000, "data": [{"trackings": [{
                "operationTime": "2026-11-03 18:00:00",
                "operationCode": "299999",
                "remark": "",
            }]}]},
            {"success": True, "code": 0, "data": {
                "status": "310", "statusDesc": "运输中",
                "updateTime": "2026-11-03 18:01:00",
            }},
        ]
        with patch.object(client, "_request", side_effect=responses) as request:
            tracks = client.query_track("JD-UNKNOWN-CODE")
        self.assertEqual(len(tracks), 2)
        self.assertIsNone(tracks[0].get("category"))
        self.assertEqual(tracks[1]["status"], "在途")
        self.assertEqual(tracks[1]["category"], "transit")
        self.assertEqual(tracks[1]["source"], "order_status")
        self.assertEqual(request.call_args_list[1].args[0], "/ecap/v1/orders/status/get")

    def test_sf_prefers_official_secondary_status_name(self):
        client = SFCourier("partner", "check", "card")
        data = {"routeResps": [{"routes": [{
            "acceptTime": "2026-09-24 16:00:00",
            "remark": "快件此前已经完成揽收，现发往下一站",
            "opCode": "99",
            "firstStatusName": "运输",
            "secondaryStatusName": "运输中",
        }]}]}
        with patch.object(settings, "sf_mock_mode", False), \
             patch.object(client, "_request", return_value={}), \
             patch.object(client, "_business_result", return_value=(True, "", data, "")):
            tracks = client.query_track("SF123")
        self.assertEqual(tracks[0]["status"], "运输中")
        self.assertEqual(tracks[0]["op_code"], "99")

    def test_deppon_prefers_human_readable_operation_name(self):
        client = DepponCourier("company", "customer", "app", "sign")
        response = {"traceList": [{
            "time": "2026-09-24 16:00:00",
            "status": "GOT",
            "opName": "已签收",
            "desc": "收件人本人签收",
        }]}
        with patch.object(settings, "db_mock_mode", False), \
             patch.object(client, "_request", return_value=response), \
             patch.object(client, "_business_result", return_value=(True, "", "")):
            tracks = client.query_track("DPK123")
        self.assertEqual(tracks[0]["status"], "已签收")


    def test_deppon_parses_production_response_param_trace_list(self):
        client = DepponCourier("company", "customer", "app", "sign")
        response = {
            "result": "true",
            "resultCode": "1000",
            "responseParam": {
                "tracking_number": "DPK123",
                "trace_list": [{
                    "time": "2026-10-13 20:19:16",
                    "status": "ARRIVAL",
                    "description": "到达厦门转运场",
                    "city": "厦门市",
                }, {
                    "time": "2026-10-13 12:18:43",
                    "status": "GOT",
                    "description": "您的订单已被收件员揽收",
                    "city": "厦门市",
                }],
            },
        }
        with patch.object(settings, "db_mock_mode", False), \
             patch.object(client, "_request", return_value=response):
            tracks = client.query_track("DPK123")
        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0]["status"], "运输中")
        self.assertEqual(tracks[0]["op_code"], "ARRIVAL")
        self.assertEqual(tracks[1]["status"], "已揽收")


if __name__ == "__main__":
    unittest.main()
