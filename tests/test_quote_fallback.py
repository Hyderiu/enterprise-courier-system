import unittest
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from api.routes.courier import (
    QuoteQuery, _base_and_insurance, _cached_quote_services,
    _quote_cache, _quote_cache_key, _quote_cache_lock, _quote_inflight,
    _quote_services, quote_services, require_selected_service_quote,
)
from config.settings import settings


class _QuoteClient:
    def __init__(self, courier_type):
        self.courier_type = courier_type
        self.calls = []

    def query_delivery_quotes(self, data):
        requested = str(data.get("business_type") or "")
        self.calls.append(requested)
        if self.courier_type == "DB":
            return {"success": True, "items": []}
        if not requested:
            return {"success": True, "items": [
                {
                    "business_type": "1", "estimated_fee": None,
                    "estimated_delivery": "2026-09-25 18:00:00",
                },
                {
                    "business_type": "2", "estimated_fee": 13,
                    "estimated_delivery": "2026-09-25 12:00:00",
                },
            ]}
        return {"success": True, "items": [{
            "business_type": requested,
            "estimated_fee": 22 if requested == "1" else 30,
            "estimated_delivery": "2026-09-25 18:00:00",
        }]}


class QuoteFallbackTests(unittest.TestCase):
    def setUp(self):
        with _quote_cache_lock:
            _quote_cache.clear()
            _quote_inflight.clear()

    def test_independent_carrier_quotes_run_concurrently(self):
        barrier = threading.Barrier(2, timeout=2)

        class _ConcurrentClient:
            def __init__(self, courier_type):
                self.courier_type = courier_type

            def query_delivery_quotes(self, data):
                if not data.get("business_type"):
                    barrier.wait()
                if self.courier_type == "SF":
                    return {"success": True, "items": [
                        {"business_type": code, "estimated_fee": 12,
                         "estimated_delivery": "2026-11-04 18:00"}
                        for code in ("1", "2")
                    ]}
                return {"success": True, "items": [
                    {"business_type": code, "estimated_fee": 10,
                     "estimated_delivery": "2026-11-05 18:00"}
                    for code in ("DJTK", "XJTK", "DJBK", "XJBK")
                ]}

        query = QuoteQuery(
            sender_province="福建省", sender_city="厦门市", sender_district="思明区",
            sender_address="示例路1号", receiver_province="福建省",
            receiver_city="福州市", receiver_district="闽侯县",
            receiver_address="创新园", item_type="文件", weight=1,
        )
        with patch(
            "courier.get_courier",
            side_effect=lambda kind, _db: _ConcurrentClient(kind),
        ):
            result = quote_services(query, None)
        self.assertTrue(any(item["quote_available"] for item in result["items"]))

    def test_carrier_filter_only_queries_requested_provider(self):
        calls = []

        class _FilteredClient:
            def __init__(self, courier_type):
                self.courier_type = courier_type
                self.timeout = 20

            def query_delivery_quotes(self, _data):
                calls.append(self.courier_type)
                return {"success": True, "items": [
                    {"business_type": "1", "estimated_fee": 12,
                     "estimated_delivery": "2026-11-04 18:00"},
                    {"business_type": "2", "estimated_fee": 10,
                     "estimated_delivery": "2026-11-05 18:00"},
                ]}

        query = QuoteQuery(
            sender_province="福建省", sender_city="厦门市", sender_district="思明区",
            sender_address="示例路1号", receiver_province="福建省",
            receiver_city="福州市", receiver_district="闽侯县",
            receiver_address="创新园", item_type="文件", weight=1,
        )
        with patch(
            "courier.get_courier",
            side_effect=lambda kind, _db: _FilteredClient(kind),
        ):
            result = _quote_services(query, None, "xiamen_other", "SF")
        self.assertEqual(len(calls), 3)
        self.assertEqual(set(calls), {"SF"})
        self.assertTrue(result["items"])
        self.assertTrue(all(item["id"].startswith("sf_") for item in result["items"]))

    def test_quote_client_timeout_is_not_forced_below_production_setting(self):
        observed_timeouts = []

        class _SlowAllowedClient:
            timeout = 20

            def query_delivery_quotes(self, data):
                observed_timeouts.append(self.timeout)
                requested = str(data.get("business_type") or "")
                rows = [
                    {"business_type": code, "estimated_fee": 12,
                     "estimated_delivery": "2026-11-08 18:00"}
                    for code in ("1", "2")
                ]
                return {"success": True, "items": [
                    row for row in rows if not requested or row["business_type"] == requested
                ]}

        query = QuoteQuery(
            sender_province="福建省", sender_city="厦门市", sender_district="思明区",
            sender_address="示例路1号", receiver_province="福建省",
            receiver_city="福州市", receiver_district="闽侯县",
            receiver_address="创新园", item_type="文件", weight=1,
        )
        with patch.object(settings, "courier_quote_timeout", 7.0), \
             patch.object(settings, "courier_quote_total_timeout", 8.0), \
             patch("courier.get_courier", return_value=_SlowAllowedClient()):
            result = _quote_services(query, None, "xiamen_other", "SF")
        self.assertTrue(any(item["quote_available"] for item in result["items"]))
        self.assertTrue(observed_timeouts)
        self.assertTrue(all(timeout > 4.5 for timeout in observed_timeouts))

    def test_identical_quote_requests_share_inflight_work(self):
        calls = []
        query = QuoteQuery(
            sender_province="福建省", sender_city="厦门市", sender_district="思明区",
            sender_address="示例路1号", receiver_province="福建省",
            receiver_city="福州市", receiver_district="闽侯县",
            receiver_address="创新园", item_type="文件", weight=1,
        )

        def slow_quote(*_args):
            calls.append(time.monotonic())
            time.sleep(0.08)
            return {"ok": True, "items": [], "recommended_id": None}

        with patch("api.routes.courier._quote_services", side_effect=slow_quote):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        _cached_quote_services, query, None, "xiamen_other", "SF"
                    )
                    for _ in range(2)
                ]
                results = [future.result() for future in futures]
        self.assertEqual(len(calls), 1)
        self.assertTrue(any(result.get("shared_inflight") for result in results))

    def test_manual_refresh_bypasses_success_cache(self):
        calls = []
        query = QuoteQuery(
            sender_province="福建省", sender_city="厦门市", sender_district="思明区",
            sender_address="示例路1号", receiver_province="福建省",
            receiver_city="福州市", receiver_district="闽侯县",
            receiver_address="创新园", item_type="文件", weight=1,
        )

        def live_quote(*_args):
            calls.append(len(calls) + 1)
            return {"ok": True, "items": [{
                "id": "sf_tekui", "quote_available": True,
                "estimated_fee": 20 + len(calls), "estimated_delivery": "明日达",
            }], "recommended_id": "sf_tekui"}

        with patch("api.routes.courier._quote_services", side_effect=live_quote):
            first = _cached_quote_services(query, None, "xiamen_other", "SF")
            cached = _cached_quote_services(query, None, "xiamen_other", "SF")
            refreshed = _cached_quote_services(
                query, None, "xiamen_other", "SF", force_refresh=True
            )
        self.assertEqual(calls, [1, 2])
        self.assertTrue(cached.get("cache_hit"))
        self.assertNotEqual(
            first["items"][0]["estimated_fee"],
            refreshed["items"][0]["estimated_fee"],
        )

    def test_route_cache_refreshes_quote_when_pickup_slot_changes_for_all_carriers(self):
        base = dict(
            sender_province="福建省", sender_city="厦门市", sender_district="思明区",
            sender_address="示例路1号", receiver_province="福建省",
            receiver_city="福州市", receiver_district="闽侯县",
            receiver_address="创新园", item_type="文件", weight=1,
        )
        first = QuoteQuery(**base, pickup_start_time="2026-11-07T08:00")
        second = QuoteQuery(**base, pickup_start_time="2026-11-08T10:00")
        for carrier in ("SF", "DB", "JDL"):
            self.assertNotEqual(
                _quote_cache_key(first, "xiamen_other", carrier),
                _quote_cache_key(second, "xiamen_other", carrier),
            )

    def test_deppon_quote_outage_does_not_disable_configured_products(self):
        class _UnavailableDeppon:
            timeout = 20

            def query_delivery_quotes(self, _data):
                return {"success": False, "items": [], "message": "上游暂时无响应"}

        query = QuoteQuery(
            sender_province="福建省", sender_city="厦门市", sender_district="思明区",
            sender_address="示例路1号", receiver_province="福建省",
            receiver_city="福州市", receiver_district="闽侯县",
            receiver_address="创新园", item_type="文件", weight=1,
        )
        with patch("courier.get_courier", return_value=_UnavailableDeppon()):
            result = _quote_services(query, None, "xiamen_other", "DB")
        self.assertTrue(result["items"])
        self.assertTrue(all(item["quote_available"] for item in result["items"]))
        self.assertTrue(all(item["quote_deferred"] for item in result["items"]))
        self.assertTrue(all("仍可选择" in item["quote_note"] for item in result["items"]))

    def test_deppon_submission_does_not_depend_on_advisory_quote_endpoint(self):
        query = QuoteQuery(
            sender_province="福建省", sender_city="厦门市", sender_district="思明区",
            sender_address="示例路1号", receiver_province="福建省",
            receiver_city="福州市", receiver_district="闽侯县",
            receiver_address="创新园", item_type="文件", weight=1,
        )
        with patch.object(settings, "db_mock_mode", False), \
             patch("api.routes.courier._quote_services") as quote:
            result = require_selected_service_quote(
                "db_small_fast", query, None, "xiamen_other"
            )
        quote.assert_not_called()
        self.assertTrue(result["quote_available"])
        self.assertTrue(result["quote_deferred"])

    def test_carrier_total_is_split_without_double_counting_insurance(self):
        self.assertEqual(_base_and_insurance(18, 1), (17.0, 1.0, 18.0))

    def test_sf_insurance_is_added_once_to_base_quote(self):
        class _Client:
            def __init__(self, kind):
                self.kind = kind

            def query_delivery_quotes(self, _data):
                if self.kind == "DB":
                    return {"success": True, "items": []}
                return {"success": True, "items": [
                    {"business_type": "1", "estimated_fee": 17, "estimated_delivery": "2026-10-24 12:00"},
                    {"business_type": "2", "estimated_fee": 15, "estimated_delivery": "2026-10-25 12:00"},
                ]}

        query = QuoteQuery(
            sender_province="福建省", sender_city="厦门市", sender_district="思明区",
            sender_address="示例路1号", receiver_province="福建省",
            receiver_city="福州市", receiver_district="闽侯县",
            receiver_address="创新园", item_type="文件", weight=1,
            insurance_amount=100,
        )
        with patch("courier.get_courier", side_effect=lambda kind, _db: _Client(kind)):
            rows = {item["id"]: item for item in quote_services(query, None)["items"]}
        self.assertEqual(rows["sf_tekui"]["estimated_fee"], 17)
        self.assertEqual(rows["sf_tekui"]["insurance_fee"], 1.0)
        self.assertEqual(rows["sf_tekui"]["estimated_total_fee"], 18.0)

    def test_deppon_inclusive_total_is_split_using_uninsured_quote(self):
        class _Client:
            def __init__(self, kind):
                self.kind = kind

            def query_delivery_quotes(self, data):
                if self.kind == "SF":
                    return {"success": True, "items": []}
                total = 21 if data.get("insurance_amount") else 20
                return {"success": True, "items": [{
                    "business_type": code, "estimated_fee": total,
                    "estimated_delivery": "2026-10-25 12:00",
                } for code in ("DJTK", "XJTK", "DJBK", "XJBK")]}

        query = QuoteQuery(
            sender_province="福建省", sender_city="厦门市", sender_district="思明区",
            sender_address="示例路1号", receiver_province="福建省",
            receiver_city="福州市", receiver_district="闽侯县",
            receiver_address="创新园", item_type="普通物品", weight=2,
            insurance_amount=100,
        )
        with patch("courier.get_courier", side_effect=lambda kind, _db: _Client(kind)):
            rows = {item["id"]: item for item in quote_services(query, None)["items"]}
        self.assertEqual(rows["db_small_standard"]["estimated_fee"], 20)
        self.assertEqual(rows["db_small_standard"]["insurance_fee"], 1)
        self.assertEqual(rows["db_small_standard"]["estimated_total_fee"], 21)

    def test_jdl_inclusive_total_is_split_using_fee_detail(self):
        class _JdlClient:
            EXPRESS_INSURANCE_PRODUCT_CODE = "ed-a-0002"
            FREIGHT_INSURANCE_PRODUCT_CODE = "fr-a-0002"

            def precheck_order(self, _data, product_code):
                return {"success": True, "data": {
                    "commonFeeInfoResponse": {
                        "totalAmount": 18,
                        "feeDetailInfoResponseList": [
                            {"productCode": product_code, "amount": 17},
                            {"productCode": "ed-a-0002", "amount": 1},
                        ],
                    },
                    "productInfos": [{"productCode": product_code}],
                }}

        query = QuoteQuery(
            sender_province="四川省", sender_city="成都市", sender_district="成华区",
            sender_address="示例路24号", receiver_province="四川省",
            receiver_city="绵阳市", receiver_district="涪城区",
            receiver_address="临园路1号", item_type="文件", weight=1,
            insurance_amount=100,
        )
        fake_db = object()
        with patch("utils.regions.enabled_carriers", return_value=["JDL"]), \
             patch("utils.regions.default_service_id", return_value="jdl_biaokuai"), \
             patch("courier.get_courier", return_value=_JdlClient()):
            result = _quote_services(query, fake_db, "chengdu")
        rows = {item["id"]: item for item in result["items"]}
        self.assertEqual(rows["jdl_biaokuai"]["estimated_fee"], 17.0)
        self.assertEqual(rows["jdl_biaokuai"]["insurance_fee"], 1.0)
        self.assertEqual(rows["jdl_biaokuai"]["estimated_total_fee"], 18.0)

    def test_beijing_catalog_excludes_same_day_and_keeps_large_selectable(self):
        class _BeijingClient:
            def __init__(self, courier_type):
                self.courier_type = courier_type

            def query_delivery_quotes(self, data):
                if self.courier_type == "DB":
                    return {"success": True, "items": []}
                requested = str(data.get("business_type") or "")
                rows = {
                    "1": {"business_type": "1", "estimated_fee": 22, "estimated_delivery": "明日达"},
                    "2": {"business_type": "2", "estimated_fee": 15, "estimated_delivery": "后日达"},
                    "6": {"business_type": "6", "estimated_fee": 13, "estimated_delivery": None},
                    "255": {"business_type": "255", "estimated_fee": 40, "estimated_delivery": "明日达"},
                }
                return {"success": True, "items": [rows[requested]] if requested else list(rows.values())}

        sf = _BeijingClient("SF")
        db = _BeijingClient("DB")
        query = QuoteQuery(
            sender_province="北京市", sender_city="北京市", sender_district="朝阳区",
            sender_address="东坝街道1号", receiver_province="北京市",
            receiver_city="北京市", receiver_district="朝阳区",
            receiver_address="东坝街道2号", item_type="普通物品", weight=20,
        )
        with patch("courier.get_courier", side_effect=lambda kind, _db: sf if kind == "SF" else db):
            result = _quote_services(query, None, "beijing")
        rows = {item["id"]: item for item in result["items"]}
        self.assertNotIn("sf_same_day", rows)
        self.assertTrue(rows["sf_large"]["quote_available"])
        self.assertEqual(result["recommended_id"], "db_small_standard")

    def test_deppon_15kg_boundary_is_advisory_not_a_selection_block(self):
        class _WeightClient:
            def __init__(self, courier_type):
                self.courier_type = courier_type

            def query_delivery_quotes(self, _data):
                if self.courier_type == "SF":
                    return {"success": True, "items": []}
                return {"success": True, "items": [
                    {"business_type": "XJTK", "estimated_fee": 12, "estimated_delivery": "明日达"},
                    {"business_type": "XJBK", "estimated_fee": 10, "estimated_delivery": "后日达"},
                    {"business_type": "DJTK", "estimated_fee": 30, "estimated_delivery": "明日达"},
                    {"business_type": "DJBK", "estimated_fee": 24, "estimated_delivery": "后日达"},
                ]}

        sf = _WeightClient("SF")
        db = _WeightClient("DB")

        def run(weight, volume=None):
            query = QuoteQuery(
                sender_province="福建省", sender_city="厦门市", sender_district="思明区",
                sender_address="示例路1号", receiver_province="福建省",
                receiver_city="福州市", receiver_district="闽侯县",
                receiver_address="创新园", item_type="普通物品", weight=weight, volume=volume,
            )
            with patch("courier.get_courier", side_effect=lambda kind, _db: sf if kind == "SF" else db):
                return {item["id"]: item for item in quote_services(query, None)["items"]}

        small = run(14.99)
        self.assertTrue(small["db_small_fast"]["quote_available"])
        self.assertTrue(small["db_small_standard"]["quote_available"])
        self.assertTrue(small["db_dajian_fast"]["quote_available"])
        self.assertNotIn("更推荐", small["db_dajian_fast"]["quote_note"])

        boundary = run(15)
        self.assertTrue(boundary["db_dajian_fast"]["quote_available"])
        self.assertTrue(boundary["db_dajian_standard"]["quote_available"])
        self.assertTrue(boundary["db_small_fast"]["quote_available"])
        self.assertNotIn("更推荐", boundary["db_small_fast"]["quote_note"])

        bulky = run(2, 0.12)
        self.assertTrue(bulky["db_dajian_fast"]["quote_available"])
        self.assertTrue(bulky["db_small_fast"]["quote_available"])
        self.assertNotIn("更推荐", bulky["db_small_fast"]["quote_note"])

    def test_missing_weight_does_not_query_carriers_or_return_prices(self):
        class _UnexpectedClient:
            def query_delivery_quotes(self, _data):
                raise AssertionError("重量为空时不应调用快递公司询价")

        query = QuoteQuery(
            sender_province="福建省", sender_city="厦门市", sender_district="思明区",
            sender_address="示例路1号", receiver_province="福建省",
            receiver_city="福州市", receiver_district="闽侯县",
            receiver_address="创新园", item_type="普通物品", weight=None,
        )
        with patch("courier.get_courier", return_value=_UnexpectedClient()):
            result = quote_services(query, None)
        self.assertEqual(result["recommended_id"], "db_small_standard")
        self.assertIn("系统默认推荐德邦小件标快", result["recommendation_reason"])
        self.assertTrue(all(not item["quote_available"] for item in result["items"]))
        self.assertTrue(all(item["estimated_fee"] is None for item in result["items"]))
        self.assertTrue(all("预估重量" in item["quote_note"] for item in result["items"]))

    def test_missing_or_incomplete_sf_products_are_queried_separately(self):
        sf = _QuoteClient("SF")
        db = _QuoteClient("DB")
        query = QuoteQuery(
            sender_province="福建省", sender_city="厦门市", sender_district="思明区",
            sender_address="示例路1号", receiver_province="福建省",
            receiver_city="福州市", receiver_district="闽侯县",
            receiver_address="创新园", item_type="文件", weight=1,
        )
        with patch("courier.get_courier", side_effect=lambda kind, _db: sf if kind == "SF" else db):
            result = quote_services(query, None)
        rows = {item["id"]: item for item in result["items"]}
        self.assertCountEqual(sf.calls, ["", "1", "2"])
        self.assertEqual(rows["sf_tekui"]["estimated_fee"], 22)
        self.assertEqual(rows["sf_biaokuai"]["estimated_fee"], 13)

    def test_incomplete_quote_does_not_change_the_configured_default(self):
        class _IncompleteClient:
            def __init__(self, courier_type):
                self.courier_type = courier_type

            def query_delivery_quotes(self, data):
                if self.courier_type == "DB":
                    return {"success": True, "items": []}
                requested = str(data.get("business_type") or "")
                if not requested:
                    return {"success": True, "items": [
                        {
                            "business_type": "1", "estimated_fee": 18,
                            "estimated_delivery": "2026-09-25 12:00:00",
                        },
                        {
                            "business_type": "2", "estimated_fee": 13,
                            "estimated_delivery": None,
                        },
                    ]}
                if requested == "1":
                    return {"success": True, "items": []}
                return {"success": True, "items": [{
                    "business_type": requested,
                    "estimated_fee": 30 if requested == "6" else 13,
                    "estimated_delivery": None,
                }]}

        sf = _IncompleteClient("SF")
        db = _IncompleteClient("DB")
        query = QuoteQuery(
            sender_province="福建省", sender_city="厦门市", sender_district="思明区",
            sender_address="示例路1号", receiver_province="福建省",
            receiver_city="福州市", receiver_district="闽侯县",
            receiver_address="创新园", item_type="普通物品", weight=1,
        )
        with patch("courier.get_courier", side_effect=lambda kind, _db: sf if kind == "SF" else db):
            result = quote_services(query, None)

        rows = {item["id"]: item for item in result["items"]}
        self.assertTrue(rows["sf_biaokuai"]["quote_available"])
        self.assertTrue(rows["sf_biaokuai"]["quote_deferred"])
        self.assertIn("费用或时效待补充", rows["sf_biaokuai"]["quote_note"])
        self.assertEqual(result["recommended_id"], "db_small_standard")
        self.assertIn("德邦小件标快", result["recommendation_reason"])

    def test_empty_advisory_quote_keeps_fixed_default_and_products_selectable(self):
        class _EmptyClient:
            def query_delivery_quotes(self, _data):
                return {"success": True, "items": []}

        query = QuoteQuery(
            sender_province="福建省", sender_city="厦门市", sender_district="思明区",
            sender_address="示例路1号", receiver_province="福建省",
            receiver_city="福州市", receiver_district="闽侯县",
            receiver_address="创新园", item_type="文件", weight=1,
        )
        with patch("courier.get_courier", return_value=_EmptyClient()):
            result = quote_services(query, None)

        rows = {item["id"]: item for item in result["items"]}
        self.assertEqual(result["recommended_id"], "db_small_standard")
        self.assertTrue(rows["sf_tekui"]["quote_available"])
        self.assertTrue(rows["sf_tekui"]["quote_deferred"])

    def test_explicit_carrier_route_rejection_disables_only_rejected_products(self):
        class _ExplicitRejectClient:
            timeout = 20

            def query_delivery_quotes(self, data):
                requested = str(data.get("business_type") or "")
                if not requested:
                    return {"success": True, "items": []}
                return {
                    "success": False,
                    "items": [],
                    "message": f"产品{requested}当前线路不支持",
                    "error_code": "ROUTE_UNSUPPORTED",
                }

        query = QuoteQuery(
            sender_province="福建省", sender_city="厦门市", sender_district="思明区",
            sender_address="示例路1号", receiver_province="福建省",
            receiver_city="福州市", receiver_district="闽侯县",
            receiver_address="创新园", item_type="文件", weight=1,
        )
        with patch("courier.get_courier", return_value=_ExplicitRejectClient()):
            result = _quote_services(query, None, "xiamen_other", "SF")
        self.assertTrue(result["items"])
        self.assertTrue(all(not item["quote_available"] for item in result["items"]))
        self.assertTrue(all("不支持" in item["quote_note"] for item in result["items"]))

if __name__ == "__main__":
    unittest.main()
