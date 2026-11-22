"""顺丰同城开放平台客户端。

签名必须使用实际发送的原始 JSON 字符串，不能在签名后再次序列化。
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any

import requests


class SFCityAPIError(RuntimeError):
    pass


class SFCityClient:
    def __init__(self, host: str, dev_id: str, dev_key: str, timeout: int = 20):
        self.host = host.rstrip("/")
        self.dev_id = str(dev_id).strip()
        self.dev_key = str(dev_key).strip()
        self.timeout = timeout
        if not self.host or not self.dev_id or not self.dev_key:
            raise ValueError("顺丰同城 host、开发者 ID 和开发者密匙均不能为空")

    @staticmethod
    def sign(body: str, dev_id: str, dev_key: str) -> str:
        source = f"{body}&{dev_id}&{dev_key}".encode("utf-8")
        md5_hex = hashlib.md5(source).hexdigest().encode("ascii")
        return base64.b64encode(md5_hex).decode("ascii")

    def request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = dict(payload)
        data["dev_id"] = int(self.dev_id) if self.dev_id.isdigit() else self.dev_id
        data["push_time"] = int(time.time())
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        signature = self.sign(body, self.dev_id, self.dev_key)
        response = requests.post(
            f"{self.host}/open/api/external/{method.lower()}",
            params={"sign": signature},
            data=body.encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        try:
            result = response.json()
        except ValueError as exc:
            raise SFCityAPIError("顺丰同城返回了非 JSON 响应") from exc
        if not isinstance(result, dict):
            raise SFCityAPIError("顺丰同城响应格式不正确")
        code = result.get("error_code")
        if code not in (None, 0, "0"):
            raise SFCityAPIError(
                f"顺丰同城请求失败 {code}: {result.get('error_msg') or result.get('message') or ''}"
            )
        return result

    def pre_create_company_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("precreateorder4c", payload)

    def pre_create_shop_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("precreateorder", payload)

    def create_company_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("createorder4c", payload)

    def create_shop_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("createorder", payload)

    def get_shop_info(self, shop_id: str, shop_type: int = 1) -> dict[str, Any]:
        return self.request("getshopinfo", {
            "shop_id": str(shop_id), "shop_type": int(shop_type),
        })

    def list_order_feed(self, sf_order_id: str) -> dict[str, Any]:
        return self.request("listorderfeed", {
            "order_id": str(sf_order_id), "order_type": 1,
        })

    def get_order_status(self, sf_order_id: str) -> dict[str, Any]:
        """查询实时状态和骑手信息，用于回调失败时补偿。"""
        return self.request("getorderstatus", {
            "order_id": str(sf_order_id), "order_type": 1,
        })

    def cancel_order(self, sf_order_id: str) -> dict[str, Any]:
        return self.request("cancelorder", {
            "order_id": str(sf_order_id),
            "order_type": 1,
            "cancel_code": 313,
            "cancel_reason": "商家发起取消",
        })

    def add_order_gratuity_fee(
        self, sf_order_id: str, shop_id: str, gratuity_fee: int,
        serial_number: str,
    ) -> dict[str, Any]:
        """Add a rider gratuity to an unassigned order; fee is in cents."""
        return self.request("addordergratuityfee", {
            "order_id": str(sf_order_id),
            "order_type": 1,
            "shop_id": str(shop_id),
            "shop_type": 1,
            "gratuity_fee": int(gratuity_fee),
            "serial_number": str(serial_number),
        })


class SFCityCourier:
    """Adapter that lets shop-mode SF City orders use the common approval flow."""

    name = "顺丰同城急送"
    STATUS_LABELS = {
        1: "寻找骑手中", 2: "已取消", 10: "骑手已接单", 12: "骑手已到店",
        15: "配送中", 17: "已送达",
    }
    STATUS_CATEGORIES = {
        1: "pending_pickup", 2: "cancelled", 10: "pending_pickup",
        12: "pending_pickup", 15: "delivering", 17: "delivered",
    }

    @staticmethod
    def _money_from_cents(data: dict[str, Any], *keys: str):
        """Read one SF City money field (the open API uses cents)."""
        for key in keys:
            value = data.get(key)
            if value in (None, ""):
                continue
            try:
                return round(float(value) / 100, 2)
            except (TypeError, ValueError):
                continue
        return None

    def __init__(self, host: str, dev_id: str, dev_key: str, shop_id: str,
                 *, timeout: int = 20, live_order_enabled: bool = False):
        self.client = SFCityClient(host, dev_id, dev_key, timeout=timeout)
        self.shop_id = str(shop_id or "").strip()
        self.live_order_enabled = bool(live_order_enabled)
        if not self.shop_id:
            raise ValueError("顺丰同城店铺 ID 不能为空")

    @staticmethod
    def _full_address(data: dict[str, Any]) -> str:
        return "".join(str(data.get(key) or "").strip() for key in (
            "receiver_province", "receiver_city", "receiver_district", "receiver_address",
        ))

    def shop_info(self) -> dict[str, Any]:
        response = self.client.get_shop_info(self.shop_id)
        return ((response.get("result") or {}).get("shop_info") or {})

    def precheck_order(self, order_data: dict[str, Any]) -> dict[str, Any]:
        insurance_amount = max(0, float(order_data.get("insurance_amount") or 0))
        declared_value = max(1, round(insurance_amount * 100)) if insurance_amount else 0
        payload = {
            "shop_id": self.shop_id,
            "shop_type": 1,
            "user_address": self._full_address(order_data),
            "user_lng": str(order_data.get("receiver_lng") or "").strip(),
            "user_lat": str(order_data.get("receiver_lat") or "").strip(),
            "city_name": str(order_data.get("receiver_city") or "").replace("市", ""),
            "weight": max(1, round(float(order_data.get("item_weight") or 1) * 1000)),
            "product_type": int(order_data.get("express_type") or 10),
            # 店铺接口仍要求商品总金额；非保价文件使用 1 分的名义金额，
            # 保价时与声明价值保持一致，避免让员工重复填写两个“价值”。
            "total_price": declared_value or 1,
            "pay_type": 1,
            "is_appoint": 0,
            "is_insured": 1 if insurance_amount else 0,
            "is_person_direct": 0,
            "lbs_type": 2,
            "return_flag": 511,
        }
        if declared_value:
            payload["declared_value"] = declared_value
        response = self.client.pre_create_shop_order(payload)
        result = response.get("result") or {}
        return {
            "success": True,
            "estimated_fee": round(float(result.get("estimate_pay_money") or 0) / 100, 2),
            "promise_delivery_minutes": result.get("promise_delivery_time"),
            "estimated_delivery": result.get("expect_time"),
            "raw_response": response,
        }

    def create_order(self, order_data: dict[str, Any]) -> dict[str, Any]:
        if not self.live_order_enabled:
            return {"success": False, "message": "顺丰同城真实下单未启用"}
        weight_gram = max(1, round(float(order_data.get("item_weight") or 1) * 1000))
        item_count = max(1, int(order_data.get("item_count") or 1))
        insurance_amount = max(0, float(order_data.get("insurance_amount") or 0))
        declared_value = max(1, round(insurance_amount * 100)) if insurance_amount else 0
        pickup_phone = str(order_data.get("sender_phone") or "").strip()
        employee_remark = str(order_data.get("item_remark") or "").strip()
        contact_remark = f"取件联系电话：{pickup_phone}"
        # 联系电话固定放在备注首行；员工填写的交接说明另起一段，避免
        # 骑士把二者读成同一项内容。接口限制 512 字符。
        combined_remark = contact_remark
        if employee_remark:
            combined_remark += f"\n\n{employee_remark}"
        payload = {
            "shop_id": self.shop_id,
            "shop_type": 1,
            "shop_order_id": str(order_data.get("order_no") or ""),
            "order_source": "企业快递系统",
            "pay_type": 1,
            "order_time": int(time.time()),
            "is_appoint": 0,
            "is_insured": 1 if insurance_amount else 0,
            "is_person_direct": 0,
            "lbs_type": 2,
            "version": 17,
            "return_flag": 511,
            "rider_pick_method": 1,
            "remark": combined_remark[:512],
            "receive": {
                "user_name": str(order_data.get("receiver_name") or "").strip(),
                "user_phone": str(order_data.get("receiver_phone") or "").strip(),
                "user_address": self._full_address(order_data),
                "user_lng": str(order_data.get("receiver_lng") or "").strip(),
                "user_lat": str(order_data.get("receiver_lat") or "").strip(),
                "city_name": str(order_data.get("receiver_city") or "").replace("市", ""),
            },
            "order_detail": {
                "total_price": declared_value or 1,
                "product_type": int(order_data.get("express_type") or 10),
                "weight_gram": weight_gram,
                "product_num": item_count,
                "product_type_num": 1,
                "product_detail": [{
                    "product_name": str(order_data.get("item_name") or "文件")[:64],
                    "product_num": item_count,
                }],
            },
        }
        if declared_value:
            payload["declared_value"] = declared_value
        response = self.client.create_shop_order(payload)
        result = response.get("result") or {}
        order_id = result.get("order_id") or result.get("sf_order_id")
        freight = self._money_from_cents(
            result, "real_pay_money", "actual_pay_money", "pay_money",
            "delivery_fee", "total_price",
        )
        return {
            "success": bool(order_id),
            "courier_order_no": str(order_id or ""),
            "courier_order_nos": [str(order_id)] if order_id else [],
            "freight": freight,
            "estimated_delivery": result.get("expect_time"),
            "message": "顺丰同城下单成功" if order_id else "顺丰同城未返回订单号",
            "raw_response": response,
        }

    def query_fee(self, sender: dict, receiver: dict, weight: float) -> dict:
        data = dict(receiver or {})
        data["item_weight"] = weight
        return self.precheck_order(data)

    @staticmethod
    def _feed_items(result: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("feed", "feeds", "order_feed", "order_feed_list", "list"):
            value = result.get(key)
            if isinstance(value, list):
                return value
        return []

    def _track_item(self, item: dict[str, Any]) -> dict[str, Any] | None:
        # listorderfeed's generic ``status`` is an operation-record field, not
        # the SF City delivery order_status.  Treating it as order_status once
        # created a false "arrived at shop" node immediately after ordering.
        raw_status = item.get("order_status")
        if raw_status is None:
            return None
        try:
            status_code = int(raw_status)
        except (TypeError, ValueError):
            status_code = 0
        timestamp = (
            item.get("status_time") or item.get("update_time")
            or item.get("create_time") or item.get("push_time")
            or int(time.time())
        )
        detail = str(
            item.get("status_desc") or item.get("status_description")
            or item.get("remark") or item.get("content") or ""
        ).strip()
        courier = item.get("rider") or item.get("courier") or item.get("operator") or {}
        return {
            "time": timestamp,
            "status": self.STATUS_LABELS.get(status_code, detail or f"状态{status_code}"),
            "detail": detail or self.STATUS_LABELS.get(status_code, "顺丰同城状态更新"),
            "category": self.STATUS_CATEGORIES.get(status_code, ""),
            "courier": {
                "name": (
                    courier.get("name") or courier.get("rider_name")
                    or item.get("rider_name") or item.get("operator_name")
                ),
                "phone": (
                    courier.get("phone") or courier.get("rider_phone")
                    or item.get("rider_phone") or item.get("operator_phone")
                ),
            },
        }

    def query_track(self, courier_order_no: str) -> list:
        """同时查实时状态和操作记录，回调遗漏时也能自动补齐。"""
        tracks, errors = [], []
        try:
            status_response = self.client.get_order_status(courier_order_no)
            status_result = status_response.get("result") or {}
            status_item = status_result
            for key in ("order", "order_info", "orderInfo", "data"):
                if isinstance(status_result.get(key), dict):
                    status_item = status_result[key]
                    break
            if isinstance(status_item, dict) and status_item.get("order_status") is not None:
                track = self._track_item(status_item)
                if track:
                    tracks.append(track)
        except Exception as exc:
            errors.append(exc)
        try:
            feed_response = self.client.list_order_feed(courier_order_no)
            feed_result = feed_response.get("result") or {}
            for item in self._feed_items(feed_result):
                track = self._track_item(item)
                if track:
                    tracks.append(track)
        except Exception as exc:
            errors.append(exc)
        if not tracks and len(errors) == 2:
            raise SFCityAPIError(f"顺丰同城状态查询失败：{errors[-1]}")
        # 实时状态可能与操作记录的最新一条重复。
        unique, seen = [], set()
        for track in tracks:
            key = (str(track.get("time")), track.get("status"), track.get("detail"))
            if key not in seen:
                seen.add(key)
                unique.append(track)
        tracks = unique
        return tracks

    def query_actual_freight(self, courier_order_no: str) -> dict:
        """Read the amount confirmed by SF City after the real order exists."""
        response = self.client.get_order_status(courier_order_no)
        result = response.get("result") or {}
        order_info = result
        for key in ("order", "order_info", "orderInfo", "data"):
            if isinstance(result.get(key), dict):
                order_info = result[key]
                break
        freight = self._money_from_cents(
            order_info,
            "real_pay_money", "actual_pay_money", "pay_money",
            "total_price", "delivery_fee", "order_price",
        )
        if freight is None and order_info is not result:
            freight = self._money_from_cents(
                result,
                "real_pay_money", "actual_pay_money", "pay_money",
                "total_price", "delivery_fee", "order_price",
            )
        return {
            "success": freight is not None,
            "freight": freight,
            "fee_detail": {"deliveryFee": freight} if freight is not None else {},
            "message": "顺丰同城费用查询成功" if freight is not None else "顺丰同城暂未返回订单金额",
            "raw_response": response,
        }

    def cancel_order(self, courier_order_no: str, order_no: str = "") -> dict:
        try:
            response = self.client.cancel_order(courier_order_no)
            return {"success": True, "message": "顺丰同城订单已取消", "raw_response": response}
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    def add_gratuity(self, courier_order_no: str, amount_yuan: int,
                     serial_number: str) -> dict:
        """Add gratuity while the order is still waiting for a rider."""
        try:
            response = self.client.add_order_gratuity_fee(
                courier_order_no, self.shop_id, int(amount_yuan) * 100,
                serial_number,
            )
            result = response.get("result") or {}
            return {
                "success": True,
                "message": f"已成功增加小费 ¥{int(amount_yuan)}",
                "gratuity_fee": round(
                    float(result.get("gratuity_fee") or int(amount_yuan) * 100) / 100, 2
                ),
                "total_gratuity_fee": round(
                    float(result.get("total_gratuity_fee") or 0) / 100, 2
                ),
                "raw_response": response,
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}
