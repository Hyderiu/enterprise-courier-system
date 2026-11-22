import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from api.routes.orders import (
    _refresh_order_tracking, _to_dict, _update_courier_assignment,
    add_sf_city_tip, cancel_order, order_can_resubmit, SFCityTipReq,
)
from api.order_dispatch import dispatch_order
from fastapi import BackgroundTasks
from database.connection import Base
from database.models import CourierTrack, Order
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from utils.order_status import (
    apply_latest_track_state, classify_order_status, classify_track_event,
    order_status_display,
)
from utils.order_export import _status_text


class OrderStatusTests(unittest.TestCase):
    def test_only_unshipped_rejected_or_failed_orders_can_be_resubmitted(self):
        self.assertTrue(order_can_resubmit(SimpleNamespace(
            status="rejected", courier_order_no=None,
        )))
        self.assertTrue(order_can_resubmit(SimpleNamespace(
            status="failed", courier_order_no="",
        )))
        self.assertFalse(order_can_resubmit(SimpleNamespace(
            status="failed", courier_order_no="SF123",
        )))
        self.assertFalse(order_can_resubmit(SimpleNamespace(
            status="success", courier_order_no="SF123",
        )))

    def test_ordered_and_carrier_already_created_wording_share_pending_pickup_label(self):
        display = order_status_display("ordered", "已下单", True)
        self.assertEqual(display, {
            "status_category": "pending_pickup",
            "status_label": "待揽收",
        })

    def test_failed_row_with_waybill_and_refusal_is_not_dispatch_failure(self):
        display = order_status_display("failed", "客户拒收，快件安排退回", True)
        self.assertEqual(display, {
            "status_category": "exception",
            "status_label": "客户拒收",
        })
        order = Order(
            id=99, order_no="CO-REFUSED", courier_type="SF",
            courier_order_no="SFDEMO000007", status="failed",
            track_status="客户拒收，快件安排退回",
            sender_name="寄件人", sender_phone="13800000000", sender_address="测试路1号",
            receiver_name="收件人", receiver_phone="13900000000",
            receiver_province="北京市", receiver_city="北京市",
            receiver_address="测试路2号", item_name="文件",
            order_response='{"code":"1000","message":"SFDEMO000007:"}',
        )
        payload = _to_dict(order)
        self.assertEqual(payload["status_label"], "客户拒收")
        self.assertFalse(payload["can_delete"])

    def test_api_detail_exposes_canonical_status_and_keeps_raw_track_text(self):
        order = Order(
            id=1, order_no="CO-STATUS-1", courier_type="DB",
            courier_order_no="DPK123", status="ordered", track_status="已下单",
            sender_name="寄件人", sender_phone="13800000000", sender_address="测试路1号",
            receiver_name="收件人", receiver_phone="13900000000",
            receiver_province="福建省", receiver_city="厦门市",
            receiver_address="测试路2号", item_name="文件",
        )
        payload = _to_dict(order)
        self.assertEqual(payload["track_status"], "已下单")
        self.assertEqual(payload["status_category"], "pending_pickup")
        self.assertEqual(payload["status_label"], "待揽收")

        latest = SimpleNamespace(track_status="运输中", track_detail="发往下一站")
        refreshed = _to_dict(order, latest)
        self.assertEqual(refreshed["track_status"], "运输中")
        self.assertEqual(refreshed["status_category"], "transit")
        self.assertEqual(refreshed["status_label"], "在途")

    def test_export_uses_same_canonical_label_instead_of_raw_carrier_wording(self):
        order = SimpleNamespace(
            status="ordered", track_status="已下单", courier_order_no="DPK123"
        )
        labels = {"pending_pickup": "待揽收"}
        self.assertEqual(
            _status_text(
                order,
                lambda item: classify_order_status(
                    item.status, item.track_status, bool(item.courier_order_no)
                ),
                labels,
            ),
            "待揽收",
        )

    def test_sf_collected_wording_is_picked(self):
        self.assertEqual(
            classify_order_status("ordered", "顺丰速运 已收取快件，感谢使用", True),
            "picked",
        )

    def test_latest_track_updates_order_summary(self):
        order = SimpleNamespace(
            status="ordered", track_status="等待揽收", courier_order_no="SF123"
        )
        category = apply_latest_track_state(order, {
            "status": "顺丰速运 已收取快件",
            "detail": "您的快件已经交给顺丰",
        })
        self.assertEqual(category, "picked")
        self.assertEqual(order.status, "success")
        self.assertEqual(order.track_status, "顺丰速运 已收取快件")

    def test_stale_pending_event_cannot_downgrade_transit_summary(self):
        order = SimpleNamespace(
            status="success", track_status="运输中", courier_order_no="SF123"
        )
        category = apply_latest_track_state(order, {
            "status": "等待收派员上门取件",
            "detail": "订单已下单",
        })
        self.assertEqual(category, "transit")
        self.assertEqual(order.status, "success")
        self.assertEqual(order.track_status, "运输中")

    def test_detail_display_cannot_downgrade_transit_to_pending_pickup(self):
        display = order_status_display(
            "success", "等待揽收", True, baseline_track_text="运输中"
        )
        self.assertEqual(display, {
            "status_category": "transit",
            "status_label": "在途",
        })

        order = Order(
            id=2, order_no="CO-STATUS-2", courier_type="SF",
            courier_order_no="SF123", status="success", track_status="运输中",
            sender_name="寄件人", sender_phone="13800000000", sender_address="测试路1号",
            receiver_name="收件人", receiver_phone="13900000000",
            receiver_province="福建省", receiver_city="厦门市",
            receiver_address="测试路2号", item_name="文件",
        )
        stale = SimpleNamespace(track_status="等待揽收", track_detail="订单已下单")
        payload = _to_dict(order, stale)
        self.assertEqual(payload["track_status"], "运输中")
        self.assertEqual(payload["status_category"], "transit")

    def test_legacy_success_without_track_text_stays_in_transit(self):
        self.assertEqual(classify_order_status("success", "", True), "transit")
        self.assertEqual(order_status_display("success", "", True)["status_label"], "在途")

    def test_mixed_pickup_and_transport_detail_uses_later_transport_stage(self):
        self.assertEqual(
            classify_track_event("", "快件已揽收，正在运送中并发往下一站"),
            "transit",
        )

    def test_in_transit_order_cannot_call_carrier_cancel_endpoint(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            order = Order(
                order_no="CO-TRANSIT-CANCEL", courier_type="SF",
                courier_order_no="SF-TRANSIT-001", status="success",
                track_status="运输中", sender_name="寄件人",
                sender_phone="13800000000", sender_address="测试路1号",
                receiver_name="收件人", receiver_phone="13900000000",
                receiver_province="福建省", receiver_city="厦门市",
                receiver_address="测试路2号", item_name="文件",
            )
            db.add(order)
            db.commit()
            db.refresh(order)
            request = SimpleNamespace(
                state=SimpleNamespace(auth_enabled=False, current_user={})
            )

            with patch("courier.get_courier_for_order") as courier_factory:
                with self.assertRaises(HTTPException) as raised:
                    cancel_order(order.id, request, db)

            self.assertEqual(raised.exception.status_code, 400)
            self.assertIn("拦截", raised.exception.detail)
            courier_factory.assert_not_called()
            db.refresh(order)
            self.assertEqual(order.status, "success")
            self.assertEqual(order.track_status, "运输中")
        finally:
            db.close()
            engine.dispose()

    def test_common_sf_and_deppon_phrases_cover_every_display_state(self):
        cases = {
            "pending_pickup": ("等待收派员上门取件", "订单已下单"),
            "picked": ("揽收成功", "快件已收取"),
            "transit": ("运输中", "快件已到达分拨中心"),
            "delivering": ("派送中", "快递员正在派件"),
            "delivered": ("签收成功", "已由前台代收"),
            "exception": ("派送异常", "因地址错误无法派送"),
            "cancelled": ("订单已取消", "客户撤单"),
        }
        for expected, (status, detail) in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(classify_track_event(status, detail), expected)

    def test_deppon_locker_notice_is_delivered_not_carrier_pickup(self):
        text = "已取件，签收人凭取货码取件"
        detail = "快件已暂存至腾讯滨海大厦B件中心丰巢柜12号"
        self.assertEqual(classify_track_event(text, detail), "delivered")
        self.assertEqual(
            order_status_display("success", f"{text} {detail}", True),
            {"status_category": "delivered", "status_label": "已签收"},
        )

    def test_sf_city_tracking_repairs_old_local_failure_after_real_order_exists(self):
        order = SimpleNamespace(
            courier_type="SF_CITY", courier_order_no="CITY-REAL-1",
            status="failed", track_status="下单失败",
        )
        category = apply_latest_track_state(order, {
            "status": "寻找骑手中", "detail": "等待骑士接单",
            "category": "pending_pickup",
        })
        self.assertEqual(category, "pending_pickup")
        self.assertEqual(order.status, "ordered")
        self.assertEqual(order.track_status, "寻找骑手中")

    def test_sf_city_placeholder_is_not_saved_as_rider_and_false_claim_is_cleared(self):
        order = SimpleNamespace(
            courier_type="SF_CITY",
            courier_picked_by="暂未有骑士接单",
            courier_picked_by_phone=None,
            sf_city_notification_events='["rider_assigned", "searching"]',
        )
        _update_courier_assignment(order, {
            "courier": {"name": "暂未有骑士接单", "phone": ""}
        })
        self.assertIsNone(order.courier_picked_by)
        self.assertEqual(order.sf_city_notification_events, '["searching"]')

        _update_courier_assignment(order, {
            "courier": {"name": "骑士甲", "phone": "13811112222"}
        })
        self.assertEqual(order.courier_picked_by, "骑士甲")
        self.assertEqual(order.courier_picked_by_phone, "13811112222")

    def test_sf_city_public_tracks_hide_marker_and_impossible_early_arrival(self):
        from api.routes.orders import _serialize_unique_tracks
        tracks = [
            {
                "time": "2026-11-09 16:27:04", "status": "配送员已到店",
                "detail": "[sfcity:052fbb9c4dac3d73f1cc3d9e] 配送员已到店 骑士:骑士甲",
            },
            {
                "time": "2026-11-09 16:24:46", "status": "配送员已指派",
                "detail": "[sfcity:4f3f3a14e9f8970ca3fcb887] 配送员已指派 骑士:骑士甲",
            },
            {
                "time": "2026-11-09 16:13:22", "status": "骑手已到店",
                "detail": "配送员已到店",
            },
            {
                "time": "2026-11-09 16:13:22", "status": "寻找骑手中",
                "detail": "下单成功，正在查找骑士",
            },
        ]
        public = _serialize_unique_tracks(tracks, "SF_CITY")
        self.assertEqual(len(public), 3)
        self.assertFalse(any("[sfcity:" in item["detail"] for item in public))
        self.assertFalse(any(
            item["status"] == "骑手已到店" and item["time"].endswith("16:13:22")
            for item in public
        ))

    def test_sf_city_detail_marks_only_unassigned_order_as_tip_eligible(self):
        order = Order(
            id=91, order_no="CITY-TIP-DETAIL", courier_type="SF_CITY",
            courier_order_no="SF-CITY-TIP-1", status="ordered",
            track_status="寻找骑手中", order_time=__import__("datetime").datetime(2026, 11, 9, 10, 0),
            sender_name="寄件人", sender_phone="13800000009", sender_address="取件地址",
            receiver_name="收件人", receiver_phone="13900000000",
            receiver_province="福建省", receiver_city="厦门市",
            receiver_address="收件地址", item_name="文件",
        )
        payload = _to_dict(order)
        self.assertTrue(payload["sf_city_waiting_for_rider"])
        self.assertEqual(payload["sf_city_wait_timeout_minutes"], 30)
        self.assertEqual(payload["sf_city_wait_deadline"], "2026-11-09T10:30:00")

        order.courier_picked_by = "骑士甲"
        self.assertFalse(_to_dict(order)["sf_city_waiting_for_rider"])

    def test_sf_city_tip_rechecks_state_then_calls_real_carrier_api(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            order = Order(
                order_no="CITY-TIP-API", courier_type="SF_CITY",
                courier_order_no="SF-CITY-TIP-API", status="ordered",
                track_status="寻找骑手中",
                sender_name="寄件人", sender_phone="13800000009", sender_address="取件地址",
                receiver_name="收件人", receiver_phone="13900000000",
                receiver_province="福建省", receiver_city="厦门市",
                receiver_address="收件地址", item_name="文件",
            )
            db.add(order)
            db.commit()
            db.refresh(order)
            courier = Mock()
            courier.add_gratuity.return_value = {
                "success": True, "message": "已成功增加小费 ¥5",
                "gratuity_fee": 5, "total_gratuity_fee": 5,
            }
            request = SimpleNamespace(state=SimpleNamespace(
                auth_enabled=False, current_user={}
            ))
            with patch(
                "api.routes.orders._refresh_order_tracking",
                return_value={"previous_status": "ordered"},
            ), patch(
                "courier.get_courier_for_order", return_value=courier,
            ):
                result = add_sf_city_tip(
                    order.id, SFCityTipReq(amount=5), request,
                    BackgroundTasks(), db,
                )
            self.assertTrue(result["ok"])
            self.assertEqual(result["total_gratuity_fee"], 5)
            self.assertEqual(courier.add_gratuity.call_args.args[:2], (
                "SF-CITY-TIP-API", 5,
            ))
        finally:
            db.close()
            engine.dispose()

    def test_concise_current_status_wins_over_historical_words_in_detail(self):
        self.assertEqual(
            classify_track_event("运输中", "快件此前已揽收，现发往下一站"),
            "transit",
        )

    def test_jdl_common_wording_maps_to_correct_progress(self):
        cases = {
            ("已揽件", "揽件员已完成取货"): "picked",
            ("分拣完成", "货物已发车前往下一站"): "transit",
            ("配送中", "配送员正在前往收件地址"): "delivering",
            ("派送完成", "客户本人签收"): "delivered",
            ("订单已关闭", "取消成功"): "cancelled",
        }
        for (status, detail), expected in cases.items():
            with self.subTest(status=status):
                self.assertEqual(classify_track_event(status, detail), expected)

    def test_terminal_database_status_is_not_downgraded_by_old_track(self):
        self.assertEqual(classify_order_status("delivered", "运输中", True), "delivered")
        self.assertEqual(classify_order_status("failed", "运输中", True), "exception")

    def test_tracking_update_survives_actual_freight_failure(self):
        order = SimpleNamespace(
            id=1, status="success", track_status="派送中",
            courier_order_no="SF123", courier_type="SF",
            actual_freight=None, actual_fee_detail=None,
        )
        courier = Mock()
        courier.query_track.return_value = [{
            "time": "2026-09-28 12:00:00",
            "status": "已签收",
            "detail": "收件人本人签收",
        }]
        courier.query_actual_freight.side_effect = RuntimeError("fee unavailable")
        with patch("courier.get_courier", return_value=courier), \
             patch("api.routes.orders._save_new_tracks", return_value=[{"status": "已签收"}]):
            result = _refresh_order_tracking(Mock(), order, include_freight=True)

        self.assertEqual(order.status, "delivered")
        self.assertEqual(order.track_status, "已签收")
        self.assertIn("实际运费查询失败", result["freight_message"])

    def test_jdl_dispatch_waybill_and_tracking_complete_the_local_chain(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            order = Order(
                order_no="CO-JDL-CHAIN", courier_type="JDL", region_code="chengdu",
                billing_account_code="CD-MONTHLY", express_type="ed-m-0001",
                express_type_name="京东标快", status="approved",
                sender_name="寄件人", sender_phone="13800000000", sender_company="测试公司",
                sender_province="四川省", sender_city="成都市", sender_district="成华区",
                sender_address="示例路24号", receiver_name="收件人",
                receiver_phone="13900000000", receiver_province="四川省",
                receiver_city="成都市", receiver_district="成华区",
                receiver_address="测试路1号", item_name="文件", item_weight=1,
                item_count=1, parcel_count=1, pickup_time_mode="immediate",
            )
            db.add(order)
            db.commit()
            db.refresh(order)

            courier = Mock()
            courier.create_order.return_value = {
                "success": True, "courier_order_no": "JD-CHAIN-001",
                "courier_order_nos": ["JD-CHAIN-001"],
                "product_name": "京东标快", "raw_response": {"code": 0},
            }
            with patch("courier.get_courier_for_order", return_value=courier):
                result = dispatch_order(order, db)
            self.assertTrue(result["ok"])
            self.assertEqual(order.status, "ordered")
            self.assertEqual(order.courier_order_no, "JD-CHAIN-001")
            self.assertEqual(
                db.query(CourierTrack).filter_by(order_id=order.id).one().track_status,
                "已下单",
            )

            courier.query_track.return_value = [{
                "time": "2026-10-21 18:00:00", "status": "配送完成",
                "detail": "收件人本人签收",
            }]
            with patch("courier.get_courier_for_order", return_value=courier):
                refreshed = _refresh_order_tracking(db, order)
                db.commit()
            self.assertEqual(refreshed["order_status"], "delivered")
            self.assertEqual(order.track_status, "配送完成")
            self.assertEqual(
                db.query(CourierTrack).filter_by(order_id=order.id).count(), 2,
            )
        finally:
            db.close()
            engine.dispose()

    def test_jdl_system_dispatch_auto_pregets_and_persists_real_waybill(self):
        from config.settings import settings
        from courier.jdl import JDLCourier

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            order = Order(
                order_no="CO-JDL-AUTO-PREGET", courier_type="JDL",
                region_code="chengdu", billing_account_code="CD-MONTHLY",
                express_type="ed-m-0001", express_type_name="京东标快",
                status="approved", sender_name="寄件人",
                sender_phone="13800000000", sender_province="四川省",
                sender_city="成都市", sender_district="成华区",
                sender_address="测试路1号", receiver_name="收件人",
                receiver_phone="13900000000", receiver_province="四川省",
                receiver_city="成都市", receiver_district="成华区",
                receiver_address="测试路2号", item_name="文件",
                item_weight=1, item_count=1, parcel_count=1,
                pickup_time_mode="immediate",
            )
            db.add(order)
            db.commit()
            db.refresh(order)

            courier = JDLCourier("app", "secret", "token", "CD-MONTHLY")
            responses = [
                {"success": True, "code": 0, "data": ["JD-SYSTEM-001"]},
                {"success": True, "code": 0, "data": {}},
            ]
            with patch.object(settings, "jdl_live_order_enabled", True), \
                 patch.object(courier, "_request", side_effect=responses) as request, \
                 patch("courier.get_courier_for_order", return_value=courier):
                result = dispatch_order(order, db)

            self.assertTrue(result["ok"])
            self.assertEqual(order.status, "ordered")
            self.assertEqual(order.courier_order_no, "JD-SYSTEM-001")
            self.assertEqual(request.call_args_list[0].args[0], "/ecap/v1/orders/pregetwaybillcodes")
            self.assertEqual(request.call_args_list[1].args[0], "/ecap/v1/orders/create")
            self.assertEqual(request.call_args_list[1].args[1]["waybillCode"], "JD-SYSTEM-001")
        finally:
            db.close()
            engine.dispose()

    def test_failed_dispatch_keeps_carrier_reason_and_uses_order_failed_label(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            order = Order(
                order_no="CO-JDL-FAILED", courier_type="JDL", region_code="chengdu",
                express_type="ed-m-0001", express_type_name="京东标快", status="approved",
                sender_name="寄件人", sender_phone="13800000000", sender_address="测试路1号",
                receiver_name="收件人", receiver_phone="13900000000",
                receiver_province="湖南省", receiver_city="长沙市",
                receiver_address="测试路2号", item_name="文件", item_weight=1,
            )
            db.add(order)
            db.commit()
            db.refresh(order)
            courier = Mock()
            courier.create_order.return_value = {
                "success": False, "message": "月结账号无下单权限",
                "error_code": "JD-403", "raw_response": {
                    "code": "JD-403", "message": "月结账号无下单权限",
                },
            }
            with patch("courier.get_courier_for_order", return_value=courier):
                result = dispatch_order(order, db)

            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "JD-403")
            self.assertEqual(result["message"], "月结账号无下单权限")
            self.assertEqual(order.status, "failed")
            payload = _to_dict(order)
            self.assertEqual(payload["status_category"], "exception")
            self.assertEqual(payload["status_label"], "下单失败")
            self.assertEqual(payload["dispatch_error_code"], "JD-403")
            self.assertEqual(payload["dispatch_error_message"], "月结账号无下单权限")
        finally:
            db.close()
            engine.dispose()

    def test_order_detail_prefers_nested_specific_failure_reason(self):
        order = Order(
            id=99, order_no="CO-NESTED-FAIL", courier_type="JDL", status="failed",
            sender_name="寄件人", sender_phone="13800000000", sender_address="测试路1号",
            receiver_name="收件人", receiver_phone="13900000000",
            receiver_province="湖南省", receiver_city="长沙市",
            receiver_address="测试路2号", item_name="文件",
            order_response=(
                '{"code":"500","message":"京东物流业务处理失败",'
                '"data":{"errorCode":"1206002",'
                '"errorMessage":"订单不存在：请核对商家订单号和月结账号"}}'
            ),
        )
        payload = _to_dict(order)
        self.assertEqual(payload["dispatch_error_code"], "1206002")
        self.assertEqual(
            payload["dispatch_error_message"],
            "订单不存在：请核对商家订单号和月结账号",
        )


if __name__ == "__main__":
    unittest.main()
