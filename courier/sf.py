"""顺丰丰桥开放平台客户端。

网关签名：Base64(MD5(msgData + timestamp + checkWord))。
生产下单必须同时满足 SF_MOCK_MODE=false 和 SF_LIVE_ORDER_ENABLED=true。
"""
import base64
import hashlib
import json
import random
import re
import time
import uuid
from datetime import datetime, timedelta

import requests

from .base import CourierClient
from config.settings import settings
from utils.http_session import pooled_post


class SFCourier(CourierClient):
    name = "顺丰速运"

    SVC_CREATE_ORDER = "EXP_RECE_CREATE_ORDER"
    SVC_SEARCH_ORDER = "EXP_RECE_SEARCH_ORDER_RESP"
    SVC_GET_SUB_MAILNO = "EXP_RECE_GET_SUB_MAILNO"
    SVC_PRE_ORDER = "EXP_RECE_PRE_ORDER"
    SVC_QUERY_TIMETM = "EXP_RECE_QUERY_DELIVERTM"
    SVC_QUERY_WAYBILL = "EXP_RECE_QUERY_SFWAYBILL"
    SVC_SEARCH_ROUTES = "EXP_RECE_SEARCH_ROUTES"
    SVC_UPDATE_ORDER = "EXP_RECE_UPDATE_ORDER"

    @staticmethod
    def _format_api_datetime(value) -> str:
        """Return the datetime format accepted by SF's order APIs."""
        formatted = str(value or "").strip().replace("T", " ")[:19]
        if len(formatted) == 16:
            formatted += ":00"
        return formatted

    def __init__(self, partner_id=None, check_word=None, monthly_card=None):
        self.api_url = settings.sf_api_url.strip()
        self.partner_id = (partner_id if partner_id is not None else settings.sf_customer_code).strip()
        self.check_word = (check_word if check_word is not None else settings.sf_check_word).strip()
        self.monthly_card = (monthly_card if monthly_card is not None else settings.sf_monthly_card).strip()
        self.timeout = settings.sf_api_timeout

    @property
    def is_mock(self) -> bool:
        return settings.sf_mock_mode

    def _configuration_error(self) -> str:
        missing = []
        if not self.api_url:
            missing.append("SF_API_URL")
        if not self.partner_id:
            missing.append("SF_CUSTOMER_CODE")
        if not self.check_word:
            missing.append("SF_CHECK_WORD")
        return "顺丰生产配置缺失：" + "、".join(missing) if missing else ""

    def _sign(self, msg_data: str, timestamp: str) -> str:
        raw = msg_data + timestamp + self.check_word
        digest = hashlib.md5(raw.encode("utf-8")).digest()
        return base64.b64encode(digest).decode("ascii")

    def _request(self, service_code: str, msg_data: dict) -> dict:
        error = self._configuration_error()
        if error:
            return {"_configuration_error": error}

        payload = dict(msg_data)
        payload.setdefault("language", "zh-CN")
        msg_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        form = {
            "serviceCode": service_code,
            "partnerID": self.partner_id,
            "requestID": uuid.uuid4().hex,
            "timestamp": timestamp,
            "msgDigest": self._sign(msg_json, timestamp),
            "msgData": msg_json,
        }
        try:
            response = pooled_post(
                self.api_url,
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            value = response.json()
            if isinstance(value, str):
                value = json.loads(value)
            if not isinstance(value, dict):
                raise ValueError("顺丰网关返回的不是 JSON 对象")
            return value
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            return {"_transport_error": f"顺丰网络或报文错误：{exc}"}

    @staticmethod
    def _business_result(response: dict) -> tuple[bool, str, dict, str]:
        """解析顺丰的网关层和业务层，绝不把 HTTP 200 当作下单成功。"""
        if response.get("_configuration_error"):
            return False, response["_configuration_error"], {}, "CONFIGURATION"
        if response.get("_transport_error"):
            return False, response["_transport_error"], {}, "NETWORK"

        outer_code = str(response.get("apiResultCode", ""))
        if outer_code not in {"A1000", "0"}:
            message = response.get("apiErrorMsg") or response.get("apiResultMsg") or "顺丰网关调用失败"
            return False, message, {}, outer_code

        inner = response.get("apiResultData")
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except json.JSONDecodeError:
                return False, "顺丰业务报文无法解析", {}, "INVALID_JSON"
        if not isinstance(inner, dict):
            return False, "顺丰返回缺少业务报文", {}, "EMPTY_DATA"

        success = inner.get("success") is True or str(inner.get("success", "")).lower() == "true"
        inner_code = str(inner.get("errorCode", ""))
        if not success:
            return False, inner.get("errorMsg") or "顺丰业务处理失败", {}, inner_code
        data = inner.get("msgData") or {}
        return True, "成功", data if isinstance(data, dict) else {}, inner_code

    @staticmethod
    def _required(d: dict, *names: str) -> str:
        missing = [name for name in names if not str(d.get(name) or "").strip()]
        return "下单信息缺失：" + "、".join(missing) if missing else ""

    def _build_create_order_payload(self, d: dict) -> dict:
        error = self._required(
            d, "order_no", "sender_name", "sender_phone", "sender_province", "sender_city",
            "sender_district", "sender_address", "receiver_name", "receiver_phone",
            "receiver_province", "receiver_city", "receiver_district", "receiver_address",
            "item_name", "item_weight",
        )
        if error:
            raise ValueError(error)
        weight = round(max(0.01, float(d.get("item_weight") or 1.0)), 2)
        express_type_id = int(d.get("express_type_id") or d.get("express_type") or 2)

        def contact(contact_type: int, prefix: str) -> dict:
            value = {
                "contactType": contact_type,
                "contact": str(d[f"{prefix}_name"]).strip(),
                "mobile": str(d[f"{prefix}_phone"]).strip(),
                "country": "CN",
                "address": str(d[f"{prefix}_address"]).strip(),
            }
            company = str(d.get(f"{prefix}_company") or "").strip()
            if company:
                value["company"] = company
            for source, target in (("province", "province"), ("city", "city"), ("district", "county")):
                part = str(d.get(f"{prefix}_{source}") or "").strip()
                if part:
                    value[target] = part
            return value

        payload = {
            "language": "zh-CN",
            "orderId": str(d["order_no"]).strip(),
            "expressTypeId": express_type_id,
            "payMethod": 1,
            "isDocall": 1,
            "parcelQty": max(1, int(d.get("parcel_count") or 1)),
            "totalWeight": weight,
            "isReturnRoutelabel": 1,
            "isGenWaybillNo": 1,
            "contactInfoList": [contact(1, "sender"), contact(2, "receiver")],
            "cargoDetails": [{"name": str(d["item_name"]).strip()}],
        }
        monthly = str(d.get("monthly_card") or self.monthly_card or "").strip()
        if monthly:
            payload["monthlyCard"] = monthly
        volume_m3 = float(d.get("item_volume") or 0)
        if volume_m3 > 0:
            payload["totalVolume"] = round(volume_m3 * 1_000_000, 3)
        insurance_amount = float(d.get("insurance_amount") or 0)
        if insurance_amount > 0:
            payload["serviceList"] = [{"name": "INSURE", "value": f"{insurance_amount:g}"}]
        if d.get("pickup_time_mode") == "appointment":
            if not d.get("pickup_start_time") or not d.get("pickup_end_time"):
                raise ValueError("预约上门必须填写开始和截止时间")
            payload["sendStartTm"] = self._format_api_datetime(d["pickup_start_time"])
            payload["extraInfoList"] = [{
                "attrName": "pickupAppointEndTime",
                "attrVal": self._format_api_datetime(d["pickup_end_time"]),
            }]
        remark = str(d.get("item_remark") or "").strip()
        if remark:
            payload["remark"] = remark
        return payload

    def _build_pre_order_payload(self, d: dict) -> dict:
        create_payload = self._build_create_order_payload(d)
        payload = {
            "orderId": create_payload["orderId"],
            "cargoName": create_payload["cargoDetails"][0]["name"],
            "contactInfoList": create_payload["contactInfoList"],
            "expressTypeId": create_payload["expressTypeId"],
            "totalWeight": create_payload["totalWeight"],
            "payMethod": create_payload["payMethod"],
            "parcelQty": create_payload["parcelQty"],
        }
        if create_payload.get("monthlyCard"):
            payload["monthlyCard"] = create_payload["monthlyCard"]
        return payload

    def precheck_order(self, order_data: dict) -> dict:
        """调用生产预下单接口；它只校验，不创建订单，也不受真实下单开关影响。"""
        try:
            payload = self._build_pre_order_payload(order_data)
        except (TypeError, ValueError) as exc:
            return {"success": False, "message": str(exc), "error_code": "LOCAL_VALIDATION"}
        response = self._request(self.SVC_PRE_ORDER, payload)
        ok, message, data, code = self._business_result(response)
        return {
            "success": ok,
            "message": "顺丰预下单校验通过" if ok else message,
            "error_code": code,
            "service_window": data,
            "raw_response": response,
        }

    @staticmethod
    def _extract_waybill_numbers(data: dict) -> tuple[str, list[str]]:
        waybills = data.get("waybillNoInfoList") or []
        raw = [str(item.get("waybillNo") or "").strip() for item in waybills]
        raw = [number for number in raw if number]
        mother = next((item for item in waybills if int(item.get("waybillType") or 0) == 1), None)
        main = str((mother or {}).get("waybillNo") or (raw[0] if raw else "")).strip()
        numbers = ([main] if main else []) + [number for number in raw if number != main]
        return main, numbers

    def query_order_result(self, order_id: str) -> dict:
        response = self._request(self.SVC_SEARCH_ORDER, {
            "searchType": "1", "orderId": order_id, "language": "zh-CN",
        })
        ok, message, data, code = self._business_result(response)
        waybill_no, waybill_numbers = self._extract_waybill_numbers(data)
        return {
            "success": ok, "message": message, "error_code": code,
            "courier_order_no": waybill_no, "waybill_numbers": waybill_numbers,
            "data": data, "raw_response": response,
        }

    def get_sub_waybills(self, order_id: str, child_count: int) -> dict:
        if child_count <= 0:
            return {"success": True, "waybill_numbers": []}
        response = self._request(self.SVC_GET_SUB_MAILNO, {
            "orderId": order_id, "parcelQty": int(child_count),
        })
        ok, message, data, code = self._business_result(response)
        _, numbers = self._extract_waybill_numbers(data)
        return {
            "success": ok, "message": message, "error_code": code,
            "waybill_numbers": numbers, "raw_response": response,
        }

    def create_order(self, order_data: dict) -> dict:
        if self.is_mock:
            return self._mock_create(order_data)
        if not settings.sf_live_order_enabled:
            return {"success": False, "message": "顺丰真实下单未启用：请确认后设置 SF_LIVE_ORDER_ENABLED=true"}
        try:
            payload = self._build_create_order_payload(order_data)
        except (TypeError, ValueError) as exc:
            return {"success": False, "message": str(exc)}
        response = self._request(self.SVC_CREATE_ORDER, payload)
        ok, message, data, code = self._business_result(response)
        recovery = None
        if not ok and code == "NETWORK":
            # A timed-out create may still have reached SF. Query by orderId before declaring failure.
            recovery = self.query_order_result(str(order_data["order_no"]))
            if recovery.get("success"):
                ok, message, data, code = True, recovery.get("message", "成功"), recovery.get("data") or {}, "S0000"
        if not ok:
            return {"success": False, "message": message, "error_code": code, "raw_response": response}

        waybill_no, waybill_numbers = self._extract_waybill_numbers(data)
        if not waybill_no:
            recovery = self.query_order_result(str(order_data["order_no"]))
            if recovery.get("success"):
                recovered_no, recovered_numbers = self._extract_waybill_numbers(recovery.get("data") or {})
                waybill_no = recovered_no or waybill_no
                waybill_numbers = recovered_numbers or waybill_numbers

        requested_count = max(1, int(order_data.get("parcel_count") or 1))
        sub_warning = ""
        if waybill_no and requested_count > len(waybill_numbers):
            child_result = self.get_sub_waybills(
                str(order_data["order_no"]), requested_count - len(waybill_numbers)
            )
            if child_result.get("success"):
                for number in child_result.get("waybill_numbers") or []:
                    if number and number not in waybill_numbers:
                        waybill_numbers.append(number)
            else:
                sub_warning = child_result.get("message") or "子单号申请失败"
        route_labels = data.get("routeLabelInfo") or []
        label_data = route_labels[0].get("routeLabelData", {}) if route_labels else {}
        filter_result = data.get("filterResult")
        try:
            filter_number = int(filter_result)
        except (TypeError, ValueError):
            filter_number = None
        if filter_number in {3, 4}:
            descriptions = {3: "不可收派", 4: "无法确定"}
            return {
                "success": False,
                "message": f"顺丰筛单结果：{descriptions[filter_number]} {data.get('remark') or ''}".strip(),
                "filter_result": filter_number,
                "raw_response": response,
            }
        return {
            "success": True,
            "courier_order_no": waybill_no,
            "courier_order_nos": waybill_numbers,
            "freight": self._estimate_freight(order_data),
            "product_name": label_data.get("proCode") or "顺丰速运",
            "dest_code": data.get("destCode") or data.get("destcode") or label_data.get("destRouteLabel", ""),
            "filter_result": filter_number,
            "estimated_delivery": data.get("sendStartTm") or "",
            "message": "顺丰已受理，等待收派员接单" + (f"，运单号 {waybill_no}" if waybill_no else "") + (f"；{sub_warning}" if sub_warning else ""),
            "warning": sub_warning,
            "raw_response": response,
        }

    def query_track(self, courier_order_no: str) -> list:
        if self.is_mock:
            return self._mock_query_track(courier_order_no)
        response = self._request(self.SVC_SEARCH_ROUTES, {
            "trackingType": "1",
            "trackingNumber": [courier_order_no],
            "methodType": "1",
        })
        ok, _, data, _ = self._business_result(response)
        if not ok:
            return []
        groups = data.get("routeResps") or data.get("routes") or []
        nodes = []
        for group in groups:
            nodes.extend(group.get("routes") or group.get("route") or [])
        result = []
        for node in nodes:
            remark = node.get("remark") or ""
            status_name = (
                node.get("secondaryStatusName") or node.get("firstStatusName")
                or node.get("opCodeText") or node.get("opName") or remark
            )
            result.append({
                "time": (node.get("acceptTime") or "").replace(" ", "T"),
                "status": status_name,
                "detail": remark,
                "op_code": node.get("opCode") or "",
                "first_status_code": node.get("firstStatusCode") or "",
                "secondary_status_code": node.get("secondaryStatusCode") or "",
                "city": node.get("acceptAddress") or "",
                "courier": self._extract_courier(remark),
            })
        return result

    def cancel_order(self, courier_order_no: str, order_no: str = "") -> dict:
        if self.is_mock:
            return {"success": True, "message": "Mock cancel ok"}
        if not settings.sf_live_order_enabled:
            return {"success": False, "message": "顺丰真实取消未启用：请确认后设置 SF_LIVE_ORDER_ENABLED=true"}
        if not order_no:
            return {"success": False, "message": "顺丰取消必须提供原始 orderId"}
        payload = {"dealType": 2, "orderId": order_no, "language": "zh-CN"}
        if courier_order_no:
            payload["waybillNoInfoList"] = [{"waybillType": 1, "waybillNo": courier_order_no}]
        response = self._request(self.SVC_UPDATE_ORDER, payload)
        ok, message, _, code = self._business_result(response)
        return {"success": ok, "message": "顺丰订单已取消" if ok else message,
                "error_code": code, "raw_response": response}

    def build_gateway_preview(self, service_code: str, msg_data: dict) -> dict:
        """Build the exact signed form structure without sending it to SF."""
        payload = dict(msg_data)
        payload.setdefault("language", "zh-CN")
        msg_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        return {
            "url": self.api_url,
            "method": "POST",
            "contentType": "application/x-www-form-urlencoded;charset=UTF-8",
            "form": {
                "serviceCode": service_code,
                "partnerID": self.partner_id,
                "requestID": uuid.uuid4().hex,
                "timestamp": timestamp,
                "msgDigest": self._sign(msg_json, timestamp) if self.check_word else "<未配置生产校验码>",
                # UI renders this as an object for readability. The wire value is compact JSON text.
                "msgData": payload,
            },
            "note": "msgData 实际以紧凑 JSON 字符串放入表单；生产校验码只参与签名，不会发送。",
        }

    def _build_delivery_quote_payload(self, order_data: dict) -> dict:
        """Build EXP_RECE_QUERY_DELIVERTM msgData from the order form."""
        required = (
            "sender_province", "sender_city", "sender_district", "sender_address",
            "receiver_province", "receiver_city", "receiver_district", "receiver_address",
        )
        missing = [name for name in required if not str(order_data.get(name) or "").strip()]
        if missing:
            raise ValueError("时效价格查询缺少完整寄收件地址")

        def address(prefix: str) -> dict:
            province = str(order_data[f"{prefix}_province"]).strip()
            city = str(order_data[f"{prefix}_city"]).strip()
            district = str(order_data[f"{prefix}_district"]).strip()
            detail = str(order_data[f"{prefix}_address"]).strip()
            return {
                "province": province,
                "city": city,
                "district": district,
                # 顺丰建议此接口的 address 同时包含省市区，以提高区域识别成功率。
                "address": province + city + district + detail,
            }

        # consignedTime is the requested handover time used by SF to calculate
        # the delivery promise.  A future slot may return no ETA on some routes;
        # the caller treats that as an unknown quote, not as route rejection.
        consigned_time = (
            self._format_api_datetime(order_data["pickup_start_time"])
            if order_data.get("pickup_start_time")
            else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        payload = {
            # 空值用于查询线路可用产品；指定值用于补查某个被聚合查询遗漏的产品。
            "businessType": str(order_data.get("business_type") or "").strip(),
            "weight": round(max(0.01, float(order_data.get("weight") or order_data.get("item_weight") or 1)), 2),
            "consignedTime": consigned_time,
            "searchPrice": "1",
            "srcAddress": address("sender"),
            "destAddress": address("receiver"),
        }
        volume_m3 = float(order_data.get("volume") or order_data.get("item_volume") or 0)
        if volume_m3 > 0:
            payload["volume"] = round(volume_m3 * 1_000_000, 2)
        return payload

    @staticmethod
    def _display_delivery_time(value) -> str:
        parts = [part.strip() for part in str(value or "").split(",") if part.strip()]
        if not parts:
            return ""
        if len(parts) == 1 or len(set(parts)) == 1:
            return parts[0]
        return f"{parts[0]} 至 {parts[-1]}"

    def query_delivery_quotes(self, order_data: dict) -> dict:
        """Query all available SF products and return normalized fee/ETA rows."""
        if self.is_mock:
            try:
                now = datetime.fromisoformat(
                    str(order_data.get("pickup_start_time") or "").replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except (TypeError, ValueError):
                now = datetime.now()
            return {
                "success": True, "message": "Mock 报价成功", "items": [
                    {"business_type": "1", "business_type_desc": "顺丰特快（Mock）",
                     "estimated_fee": 22.0,
                     "estimated_delivery": (now + timedelta(days=1)).strftime("%Y-%m-%d 18:00:00"),
                     "currency": "CNY"},
                    {"business_type": "2", "business_type_desc": "顺丰标快（Mock）",
                     "estimated_fee": 16.0,
                     "estimated_delivery": (now + timedelta(days=2)).strftime("%Y-%m-%d 18:00:00"),
                     "currency": "CNY"},
                    {"business_type": "6", "business_type_desc": "顺丰即日（Mock）",
                     "estimated_fee": 32.0,
                     "estimated_delivery": now.replace(hour=23, minute=59, second=0).strftime("%Y-%m-%d %H:%M:%S"),
                     "currency": "CNY"},
                ], "request": {}, "raw_response": {"mock": True},
            }
        try:
            payload = self._build_delivery_quote_payload(order_data)
        except (TypeError, ValueError) as exc:
            return {"success": False, "message": str(exc), "items": [], "request": {}}
        response = self._request(self.SVC_QUERY_TIMETM, payload)
        ok, message, data, code = self._business_result(response)
        if not ok:
            return {
                "success": False, "message": message, "error_code": code,
                "items": [], "request": payload, "raw_response": response,
            }
        rows = data.get("deliverTmDto") or []
        if isinstance(rows, dict):
            rows = [rows]
        items = []
        for row in rows:
            fee = row.get("fee")
            try:
                fee = float(fee) if fee is not None and str(fee).strip() else None
                if fee is not None and fee <= 0:
                    fee = None
            except (TypeError, ValueError):
                fee = None
            items.append({
                "business_type": str(row.get("businessType") or ""),
                "business_type_desc": str(row.get("businessTypeDesc") or ""),
                "estimated_fee": fee,
                "estimated_delivery": self._display_delivery_time(row.get("deliverTime")),
                "currency": row.get("currency") or "CNY",
            })
        return {
            "success": True, "message": "顺丰时效价格查询成功",
            "items": items, "request": payload, "raw_response": response,
        }

    def query_actual_freight(self, courier_order_no: str) -> dict:
        if self.is_mock:
            return {"success": False, "message": "Mock 模式没有真实运费"}
        response = self._request(self.SVC_QUERY_WAYBILL, {
            "trackingType": "2", "trackingNum": courier_order_no,
        })
        ok, message, data, _ = self._business_result(response)
        if not ok:
            return {"success": False, "message": message, "raw_response": response}
        fees = data.get("waybillFeeList") or data.get("WaybillFeeList") or []
        total = 0.0
        found = False
        for item in fees:
            try:
                total += float(item.get("value"))
                found = True
            except (TypeError, ValueError):
                continue
        return {
            "success": True, "freight": round(total, 2) if found else None,
            "fee_items": fees, "waybill_info": data.get("waybillInfo") or data.get("WaybillInfo"),
            "message": "顺丰清单运费查询成功" if found else "顺丰尚未返回清单费用",
            "raw_response": response,
        }

    def query_fee(self, sender: dict, receiver: dict, weight: float) -> dict:
        return {"success": False, "freight": None,
                "message": "未取得顺丰月结线路报价，不能使用固定首续重伪造价格"}

    def _estimate_freight(self, d: dict):
        return None

    @staticmethod
    def _extract_courier(remark: str) -> dict:
        if not remark:
            return {"name": "", "phone": ""}
        name_match = re.search(r"(?:快递员|揽收员|派件员|收派员|小哥)[：: ]?([\u4e00-\u9fa5A-Za-z]{2,8})", remark)
        phone_match = re.search(r"1[3-9]\d{9}", remark)
        return {"name": name_match.group(1) if name_match else "", "phone": phone_match.group(0) if phone_match else ""}

    def _mock_create(self, d: dict) -> dict:
        now = datetime.now()
        base = "SFMOCK" + now.strftime("%Y%m%d%H%M%S") + str(random.randint(100, 999))
        count = max(1, int(d.get("parcel_count") or 1))
        waybills = [base] + [f"{base}-{index}" for index in range(1, count)]
        product_names = {
            1: "顺丰特快", 2: "顺丰标快", 6: "顺丰即日", 255: "顺丰大件",
        }
        express_type_id = int(d.get("express_type_id") or d.get("express_type") or 2)
        product_name = product_names.get(express_type_id, "顺丰速运")
        estimated_delivery = (
            now.replace(hour=23, minute=59, second=0, microsecond=0)
            if express_type_id == 6 else now + timedelta(days=1)
        )
        return {
            "success": True, "courier_order_no": waybills[0], "courier_order_nos": waybills,
            "freight": self._estimate_freight(d), "product_name": f"{product_name}（Mock）",
            "dest_code": "MOCK", "filter_result": 2,
            "estimated_delivery": estimated_delivery.strftime("%Y-%m-%d %H:%M:%S"),
            "message": "Mock 下单成功，不会通知真实快递员", "raw_response": {"mock": True},
        }

    @staticmethod
    def _mock_query_track(_: str) -> list:
        now = datetime.now()
        return [{"time": now.isoformat(timespec="seconds"), "status": "Mock 已下单",
                 "detail": "模拟轨迹，不是真实顺丰状态", "op_code": "MOCK", "city": "",
                 "courier": {"name": "", "phone": ""}}]
