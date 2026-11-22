"""京东物流“京东快递快运”开放接口客户端。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from .base import CourierClient
from config.settings import settings
from utils.order_failure import extract_order_failure
from utils.http_session import pooled_post


class JDLCourier(CourierClient):
    name = "京东物流"
    PRODUCT_NAMES = {
        "ed-m-0001": "京东标快",
        "ed-m-0002": "京东特快",
        "fr-m-0004": "重货标快",
        "fr-m-0007": "重货特快",
        "fr-m-0017": "重货特惠",
    }
    EXPRESS_INSURANCE_PRODUCT_CODE = "ed-a-0002"
    FREIGHT_INSURANCE_PRODUCT_CODE = "fr-a-0002"
    # 已通过生产轨迹回查确认的京东标准轨迹码。其他数字码仍会依据
    # operationDesc/remark 文本归类，并保留在 op_code 便于后续补充。
    TRACK_CODE_CATEGORIES = {
        "200010": "cancelled",       # 寄件人已取消寄件
        "200034": "pending_pickup",  # 已安排快递员上门取件
    }
    ORDER_STATUS_CODE_CATEGORIES = {
        "690": "cancelled",  # 已取消（生产状态查询已验证）
    }

    def __init__(
        self,
        app_key: str | None = None,
        app_secret: str | None = None,
        access_token: str | None = None,
        customer_code: str | None = None,
        business_unit_code: str | None = None,
    ):
        self.base_url = settings.jdl_api_url.rstrip("/")
        self.app_key = (app_key if app_key is not None else settings.jdl_app_key).strip()
        self.app_secret = (app_secret if app_secret is not None else settings.jdl_app_secret).strip()
        self.access_token = (
            access_token if access_token is not None else settings.jdl_access_token
        ).strip()
        self.customer_code = (
            customer_code if customer_code is not None else settings.jdl_customer_code
        ).strip()
        self.business_unit_code = (
            business_unit_code
            if business_unit_code is not None
            else settings.jdl_business_unit_code
        ).strip()
        self.timeout = settings.jdl_api_timeout

    def _configuration_error(self) -> str:
        missing = []
        for value, label in (
            (self.app_key, "JDL_APP_KEY"),
            (self.app_secret, "JDL_APP_SECRET"),
            (self.access_token, "JDL_ACCESS_TOKEN"),
            (self.customer_code, "JDL_CUSTOMER_CODE"),
        ):
            if not value:
                missing.append(label)
        return "京东物流生产配置缺失：" + "、".join(missing) if missing else ""

    def _sign(self, path: str, param_json: str, timestamp: str) -> str:
        content = (
            f"{self.app_secret}"
            f"access_token{self.access_token}"
            f"app_key{self.app_key}"
            f"method{path}"
            f"param_json{param_json}"
            f"timestamp{timestamp}"
            f"v2.0"
            f"{self.app_secret}"
        )
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    def _request(
        self,
        path: str,
        payload: dict | list[dict],
        *,
        lop_dn: str = "ECAP",
    ) -> dict:
        error = self._configuration_error()
        if error:
            return {"_transport_error": error}
        body = payload if isinstance(payload, list) else [payload]
        param_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        query = {
            "app_key": self.app_key,
            "access_token": self.access_token,
            "timestamp": timestamp,
            "v": "2.0",
            "LOP-DN": lop_dn,
            "sign": self._sign(path, param_json, timestamp),
        }
        try:
            response = pooled_post(
                f"{self.base_url}{path}", params=query, data=param_json.encode("utf-8"),
                headers={
                    "Content-Type": "application/json;charset=UTF-8",
                    "lop-tz": "8",
                    "User-Agent": "courier-system/jdl-python",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError("京东物流返回的不是 JSON 对象")
            return value
        except requests.HTTPError as exc:
            # requests 的默认异常文本包含完整 URL，而京东把 access_token
            # 放在查询参数中。这里只保留状态码，避免凭证进入日志或页面。
            status = exc.response.status_code if exc.response is not None else "未知"
            return {"_transport_error": f"京东物流网络错误：HTTP {status}"}
        except requests.RequestException as exc:
            return {"_transport_error": f"京东物流网络错误：{exc.__class__.__name__}"}
        except (ValueError, json.JSONDecodeError) as exc:
            return {"_transport_error": f"京东物流报文错误：{exc}"}

    @staticmethod
    def _business_result(response: dict) -> tuple[bool, str, str, Any]:
        if response.get("_transport_error"):
            return False, response["_transport_error"], "NETWORK", None
        code = str(response.get("code") if response.get("code") is not None else "")
        success = response.get("success") is True or code in {"0", "200", "1000", "0000"}
        if success:
            message = str(
                response.get("subMsg") or response.get("message") or response.get("msg")
                or response.get("errorMessage") or "成功"
            )
        else:
            failure = extract_order_failure(response)
            code = failure["code"] or code
            message = failure["message"]
        return success, message, code, response.get("data")

    @staticmethod
    def _full_address(data: dict, prefix: str) -> str:
        parts = [
            data.get(f"{prefix}_province"), data.get(f"{prefix}_city"),
            data.get(f"{prefix}_district"), data.get(f"{prefix}_address"),
        ]
        return "".join(str(part or "").strip() for part in parts)

    @staticmethod
    def _origin_for_product(product_code: str) -> int:
        # 京东物流 ECAP 主产品中，fr-* 为快运 KYB2C；快递 ed-* 为 B2C。
        # lq-* 是其他平台的产品编码，不能用于京东物流 ECAP。
        return 4 if str(product_code).startswith("fr-") else 1

    @staticmethod
    def _find_first(value: Any, keys: tuple[str, ...]) -> str:
        """兼容京东不同版本的响应包装，提取首个非空运单号。"""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in keys:
                found = value.get(key)
                if isinstance(found, list):
                    found = next((item for item in found if str(item or "").strip()), "")
                if found is not None and str(found).strip():
                    return str(found).strip()
            for child in value.values():
                # 普通字符串可能是提示语，只在返回体本身或列表元素为字符串时
                # 才将其视为运单号；字典内仍只认明确字段或嵌套结构。
                if isinstance(child, (dict, list)):
                    found = JDLCourier._find_first(child, keys)
                    if found:
                        return found
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, str) and child.strip():
                    return child.strip()
                if isinstance(child, (dict, list)):
                    found = JDLCourier._find_first(child, keys)
                    if found:
                        return found
        return ""

    @staticmethod
    def _trace_records(value: Any) -> list[dict]:
        """Find the carrier event list without mistaking cargo/product arrays for traces."""
        trace_keys = (
            "traceDetails", "traceDetailList", "traceList", "traces",
            "trackings", "logisticsTrace", "logisticsTraceList", "nodes", "routeList",
        )
        if isinstance(value, dict):
            for key in trace_keys:
                candidate = value.get(key)
                if isinstance(candidate, list) and any(isinstance(item, dict) for item in candidate):
                    return [item for item in candidate if isinstance(item, dict)]
            for child in value.values():
                found = JDLCourier._trace_records(child)
                if found:
                    return found
        elif isinstance(value, list):
            # Some versions return the event list directly.  Require an event-like
            # key so unrelated lists in the response are not exposed as tracking.
            event_keys = {
                "operationTime", "operatorTime", "operateTime", "msgTime", "time",
                "createTime", "operationDesc", "traceMessage", "secondaryStatusName",
                "statusName", "operationTitle", "context", "description",
            }
            records = [item for item in value if isinstance(item, dict)]
            if records and any(event_keys.intersection(item) for item in records):
                return records
            for child in value:
                found = JDLCourier._trace_records(child)
                if found:
                    return found
        return []

    @staticmethod
    def _trace_value(record: dict, keys: tuple[str, ...]) -> str:
        for key in keys:
            value = record.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    @classmethod
    def _normalize_trace_data(cls, value: Any) -> list[dict]:
        """Convert JD Logistics trace variants to the system-wide event contract."""
        from utils.order_status import classify_track_event

        normalized = []
        for record in cls._trace_records(value):
            event_time = cls._trace_value(record, (
                "operationTime", "operatorTime", "operateTime", "msgTime", "time",
                "createTime", "scanTime", "occurTime",
            ))
            raw_status = cls._trace_value(record, (
                "secondaryStatusName", "statusName", "operationTitle", "nodeName",
                "stateName", "status", "state", "operationCode",
            ))
            detail = cls._trace_value(record, (
                "operationDesc", "traceMessage", "context", "description", "remark",
                "message", "content", "operateDesc",
            ))
            location = cls._trace_value(record, (
                "operateSite", "siteName", "currentSite", "cityName", "location",
            ))
            if location and location not in detail:
                detail = f"{location} · {detail}" if detail else location
            status = raw_status or detail
            if not detail:
                detail = status
            if event_time or status or detail:
                category = cls.TRACK_CODE_CATEGORIES.get(raw_status)
                if not category:
                    category = classify_track_event(status, detail)
                if raw_status.isdigit() and category:
                    from utils.order_status import ORDER_STATUS_LABELS
                    status = ORDER_STATUS_LABELS.get(category, status)
                item = {
                    "time": event_time,
                    "status": status,
                    "detail": detail,
                    "op_code": raw_status if raw_status.isdigit() else "",
                    "raw": record,
                }
                if category:
                    item["category"] = category
                normalized.append(item)
        return normalized

    def _build_precheck_payload(self, data: dict, product_code: str = "") -> dict:
        weight = round(max(0.01, float(data.get("item_weight") or data.get("weight") or 1)), 2)
        volume_m3 = float(data.get("item_volume") or data.get("volume") or 0)
        parcel_count = max(1, int(data.get("parcel_count") or 1))
        insurance_amount = round(max(0, float(data.get("insurance_amount") or 0)), 2)
        order_origin = self._origin_for_product(product_code)
        sender_contact = {
            "fullAddress": self._full_address(data, "sender"),
            "name": str(data.get("sender_name") or "").strip(),
            "mobile": str(data.get("sender_phone") or "").strip(),
        }
        receiver_contact = {
            "fullAddress": self._full_address(data, "receiver"),
            "name": str(data.get("receiver_name") or "").strip(),
            "mobile": str(data.get("receiver_phone") or "").strip(),
        }
        if str(data.get("sender_company") or "").strip():
            sender_contact["company"] = str(data["sender_company"]).strip()
        if str(data.get("receiver_company") or "").strip():
            receiver_contact["company"] = str(data["receiver_company"]).strip()
        payload = {
            "senderContact": sender_contact,
            "receiverContact": receiver_contact,
            "orderOrigin": order_origin,
            "cargoes": [{
                "name": str(data.get("item_name") or "文件").strip(),
                # 京东官方定义 quantity 为件数（包裹数），不是每包物品数量。
                "quantity": parcel_count,
                "weight": weight,
                "remark": str(data.get("item_remark") or "").strip(),
            }],
        }
        if order_origin == 1:
            payload["customerCode"] = self.customer_code
            payload["CommonChannelInfo"] = {"channelCode": "0030001"}
        elif self.business_unit_code:
            payload["businessUnitCode"] = self.business_unit_code
        if volume_m3 > 0:
            payload["cargoes"][0]["volume"] = round(volume_m3 * 1_000_000, 2)
        if product_code:
            products_req = {"productCode": product_code}
            if insurance_amount > 0:
                insurance_code = (
                    self.FREIGHT_INSURANCE_PRODUCT_CODE
                    if product_code.startswith("fr-")
                    else self.EXPRESS_INSURANCE_PRODUCT_CODE
                )
                products_req["addedProducts"] = [{
                    "productCode": insurance_code,
                    "productAttrs": {"guaranteeMoney": self._money_text(insurance_amount)},
                }]
            payload["productsReq"] = products_req
        return payload

    @staticmethod
    def _money_text(value: float) -> str:
        return f"{value:.2f}".rstrip("0").rstrip(".")

    @staticmethod
    def _epoch_millis(value: Any) -> int | None:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return None
        if parsed.tzinfo is None:
            # 页面预约时间按中国标准时间填写，不能依赖部署服务器所在时区。
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        return int(parsed.timestamp() * 1000)

    def precheck_order(self, order_data: dict, product_code: str = "") -> dict:
        if self._origin_for_product(product_code) == 4 and not self.business_unit_code:
            return {
                "success": False,
                "message": "京东物流大件配置缺失：快运事业部编码（businessUnitCode）",
                "error_code": "CONFIG",
                "data": None,
            }
        response = self._request(
            "/ecap/v1/orders/precheck",
            self._build_precheck_payload(order_data, product_code),
        )
        ok, message, code, data = self._business_result(response)
        return {"success": ok, "message": message, "error_code": code, "data": data}

    def preget_waybill(self, order_origin: int = 1, sum_: int = 1) -> dict:
        response = self._request("/ecap/v1/orders/pregetwaybillcodes", {
            "orderOrigin": order_origin,
            "customerCode": self.customer_code,
            "sum": max(1, min(500, int(sum_))),
        })
        ok, message, code, data = self._business_result(response)
        return {"success": ok, "message": message, "error_code": code, "data": data}

    def _build_create_order_payload(self, order_data: dict, product_code: str) -> dict:
        payload = self._build_precheck_payload(order_data, product_code)
        payload.update({
            "orderId": str(order_data.get("order_no") or "").strip(),
            # 当前月结账号的前置校验 settlementTypeList 返回 3（月结）。
            "settleType": int(order_data.get("settle_type") or 3),
            "pickupType": 1,
            "remark": str(order_data.get("item_remark") or "").strip(),
        })
        pickup_start = self._epoch_millis(order_data.get("pickup_start_time"))
        pickup_end = self._epoch_millis(order_data.get("pickup_end_time"))
        if pickup_start is not None:
            payload["pickupStartTime"] = pickup_start
        if pickup_end is not None:
            payload["pickupEndTime"] = pickup_end
        if order_data.get("waybill_code"):
            payload["waybillCode"] = str(order_data["waybill_code"]).strip()
        return payload

    def create_order(self, order_data: dict) -> dict:
        if not settings.jdl_live_order_enabled:
            return {"success": False, "message": "京东物流真实下单未启用"}
        product_code = str(order_data.get("express_type") or "ed-m-0001").strip()
        order_origin = self._origin_for_product(product_code)
        if order_origin == 4 and not self.business_unit_code:
            return {
                "success": False,
                "message": "京东物流大件配置缺失：快运事业部编码（businessUnitCode）",
                "error_code": "CONFIG",
            }
        prepared_order = dict(order_data)
        waybill_code = str(prepared_order.get("waybill_code") or "").strip()
        waybill_pregeted = False
        if not waybill_code:
            preget = self.preget_waybill(order_origin=order_origin, sum_=1)
            if not preget.get("success"):
                return {
                    "success": False,
                    "message": f"京东运单号预取失败：{preget.get('message') or '未知错误'}",
                    "error_code": preget.get("error_code") or "PREGET_FAILED",
                    "data": preget.get("data"),
                    "raw_response": preget,
                }
            waybill_code = self._find_first(
                preget.get("data"),
                ("waybillCode", "waybillCodes", "deliveryId", "waybillNo"),
            )
            if not waybill_code:
                return {
                    "success": False,
                    "message": "京东运单号预取成功，但返回中没有可用运单号",
                    "error_code": "EMPTY_WAYBILL",
                    "data": preget.get("data"),
                    "raw_response": preget,
                }
            prepared_order["waybill_code"] = waybill_code
            waybill_pregeted = True

        payload = self._build_create_order_payload(prepared_order, product_code)
        response = self._request("/ecap/v1/orders/create", payload)
        ok, message, code, data = self._business_result(response)
        returned_waybill = self._find_first(
            data, ("waybillCode", "deliveryId", "waybillNo", "waybillCodes")
        ) or waybill_code
        recovery_used = False
        if not ok and str(code) == "NETWORK" and returned_waybill:
            # 创建请求超时并不代表京东未受理；用已预取运单号回查，避免
            # 将实际已创建的订单误标为下单失败并被员工重复提交。
            recovered = self.query_order("", returned_waybill, order_origin)
            if recovered.get("success"):
                ok = True
                message = "京东创建响应异常，但按运单号回查已确认下单成功"
                code = str(recovered.get("error_code") or "0")
                data = recovered.get("data")
                recovery_used = True
        return {
            "success": ok, "message": message, "error_code": code,
            "data": data, "product_name": self.PRODUCT_NAMES.get(product_code, product_code),
            "courier_order_no": returned_waybill or None,
            "courier_order_nos": [returned_waybill] if returned_waybill else [],
            "waybill_pregeted": waybill_pregeted,
            "recovery_used": recovery_used,
            "raw_response": response,
        }

    def query_order(self, order_no: str = "", courier_order_no: str = "", order_origin: int = 1) -> dict:
        payload = {
            "waybillCode": courier_order_no,
            "orderCode": order_no,
            "orderOrigin": order_origin,
            "customerCode": self.customer_code,
        }
        response = self._request("/ecap/v1/orders/status/get", payload)
        ok, message, code, data = self._business_result(response)
        fallback_used = False
        if not ok and courier_order_no and order_no and str(code) == "1206002":
            fallback_payload = dict(payload)
            fallback_payload["orderCode"] = ""
            response = self._request("/ecap/v1/orders/status/get", fallback_payload)
            ok, message, code, data = self._business_result(response)
            fallback_used = True
        return {
            "success": ok, "message": message, "error_code": code, "data": data,
            "fallback_used": fallback_used,
        }

    def _order_status_track(self, courier_order_no: str) -> dict | None:
        """Convert the status endpoint into one canonical tracking event."""
        status_result = self.query_order("", courier_order_no, 1)
        if not status_result.get("success"):
            return None
        status_data = status_result.get("data")
        status_code = self._find_first(
            status_data, ("status", "statusCode", "orderStatus", "state")
        )
        status_desc = self._find_first(
            status_data,
            ("statusDesc", "statusName", "orderStatusDesc", "stateDesc", "statusMessage"),
        )
        event_time = self._find_first(
            status_data,
            ("updateTime", "statusTime", "operateTime", "operationTime", "createTime"),
        )
        category = self.ORDER_STATUS_CODE_CATEGORIES.get(status_code)
        if not category:
            from utils.order_status import classify_track_event
            category = classify_track_event(status_desc, status_desc)
        if not status_code and not status_desc:
            return None
        from utils.order_status import ORDER_STATUS_LABELS
        status_label = ORDER_STATUS_LABELS.get(category, status_desc or status_code)
        item = {
            "time": event_time,
            "status": status_label,
            "detail": status_desc or status_label,
            "op_code": status_code,
            "source": "order_status",
        }
        if category:
            item["category"] = category
        return item

    def _supplement_unknown_tracks(self, tracks: list[dict], courier_order_no: str) -> list[dict]:
        """Use order status only when a returned numeric trace cannot be classified."""
        if tracks and all(item.get("category") for item in tracks):
            return tracks
        status_track = self._order_status_track(courier_order_no)
        if not status_track:
            return tracks
        if not tracks:
            return [status_track]
        duplicate = any(
            item.get("op_code") == status_track.get("op_code")
            and item.get("status") == status_track.get("status")
            for item in tracks
        )
        if not duplicate:
            tracks.append(status_track)
        return tracks

    def query_track(self, courier_order_no: str) -> list:
        # 官方已停止维护旧 /ecap/v1/orders/trace/query，新接入应申请
        # “京东物流标准轨迹服务”的 /jd/tracking/query（API 2184）。
        response = self._request(
            "/jd/tracking/query",
            {
                "referenceNumber": courier_order_no,
                "referenceType": "20000",
                "customerCode": self.customer_code,
                "scope": 2,
            },
            lop_dn="Tracking_JD",
        )
        ok, _, _, data = self._business_result(response)
        tracks = self._normalize_trace_data(data) if ok else []
        if tracks:
            return self._supplement_unknown_tracks(tracks, courier_order_no)

        # 兼容已开通过渡期旧权限的存量应用；新接口开通并返回轨迹后不会调用。
        legacy_response = self._request("/ecap/v1/orders/trace/query", {
            "waybillCode": courier_order_no,
            "orderCode": "",
            "orderOrigin": 1,
            "customerCode": self.customer_code,
        })
        legacy_ok, _, _, legacy_data = self._business_result(legacy_response)
        legacy_tracks = self._normalize_trace_data(legacy_data) if legacy_ok else []
        if legacy_tracks:
            return self._supplement_unknown_tracks(legacy_tracks, courier_order_no)

        # 部分刚创建或刚取消的订单，轨迹服务短时间没有节点，但订单状态
        # 已经更新。此时用运单号读取状态，避免页面长期停留在“待揽收”。
        return self._supplement_unknown_tracks([], courier_order_no)

    @staticmethod
    def _fee_info(value: Any) -> dict:
        """Find the ECAP fee object without depending on one response wrapper."""
        if isinstance(value, dict):
            direct = value.get("commonFeeInfoResponse")
            if isinstance(direct, dict):
                return direct
            if (
                "feeDetailInfoResponseList" in value
                or "commonActualFeeInfoDetails" in value
                or any(key in value for key in (
                "totalAmount", "settleAmount", "payableAmount", "receivableAmount",
                "totalFee", "freightAmount", "freight", "sumMoney",
                "totalAfterBeforehandDiscount", "totalStandardAmount",
                ))
            ):
                return value
            for child in value.values():
                found = JDLCourier._fee_info(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = JDLCourier._fee_info(child)
                if found:
                    return found
        return {}

    @staticmethod
    def _fee_number(value) -> float | None:
        try:
            if value is None or str(value).strip() == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def query_actual_freight(self, courier_order_no: str) -> dict:
        """Read the carrier-weighed fee from JD Logistics' dedicated fee API."""
        if not courier_order_no:
            return {"success": False, "message": "京东运费查询缺少运单号"}
        response = self._request("/ecap/v1/orders/actualfee/query", {
            "waybillCode": courier_order_no,
            "orderCode": "",
            "orderOrigin": 1,
            "customerCode": self.customer_code,
        })
        ok, message, code, data = self._business_result(response)
        if not ok:
            return {
                "success": False,
                "message": message or "京东实际费用查询失败",
                "error_code": code or "",
            }
        fee_info = self._fee_info(data)
        details = (
            fee_info.get("commonActualFeeInfoDetails")
            or fee_info.get("feeDetailInfoResponseList")
            or []
        )
        if not isinstance(details, list):
            details = []
        freight = None
        for key in (
            "totalAfterBeforehandDiscount", "sumMoney",
            "totalAmount", "settleAmount", "payableAmount", "receivableAmount",
            "totalFee", "freightAmount", "freight", "totalStandardAmount",
        ):
            freight = self._fee_number(fee_info.get(key))
            if freight is not None:
                break
        if freight is None and details:
            amounts = [
                self._fee_number(
                    item.get("afterBeforehandDiscount")
                    if item.get("afterBeforehandDiscount") is not None
                    else item.get("money")
                    if item.get("money") is not None
                    else item.get("amount")
                )
                for item in details if isinstance(item, dict)
            ]
            numeric = [amount for amount in amounts if amount is not None]
            if numeric:
                freight = round(sum(numeric), 2)
        return {
            "success": True,
            "freight": freight,
            "fee_items": details,
            "fee_detail": fee_info,
            "message": "京东运费查询成功" if freight is not None else "京东尚未返回实际运费",
        }

    def cancel_order(self, courier_order_no: str, order_no: str = "") -> dict:
        if not settings.jdl_live_order_enabled:
            return {"success": False, "message": "京东物流真实取消未启用"}
        payload = {
            "waybillCode": courier_order_no,
            "orderCode": order_no,
            "orderOrigin": 1,
            "cancelType": 1,
            "customerCode": self.customer_code,
            "cancelReason": "客户申请取消寄件订单",
            "cancelReasonCode": "1",
        }
        response = self._request("/ecap/v1/orders/cancel", payload)
        ok, message, code, data = self._business_result(response)
        fallback_used = False
        # 京东标快生产接口可能只认预取的运单号，不认调用方自定义的
        # orderId。组合定位返回“订单不存在”时自动回退为仅运单号取消。
        if not ok and courier_order_no and str(code) == "1206002" and order_no:
            fallback_payload = dict(payload)
            fallback_payload["orderCode"] = ""
            response = self._request("/ecap/v1/orders/cancel", fallback_payload)
            ok, message, code, data = self._business_result(response)
            fallback_used = True
        return {
            "success": ok, "message": message, "error_code": code, "data": data,
            "fallback_used": fallback_used,
        }

    def query_fee(self, sender: dict, receiver: dict, weight: float) -> dict:
        order_data = {**{f"sender_{k}": v for k, v in sender.items()},
                      **{f"receiver_{k}": v for k, v in receiver.items()},
                      "weight": weight, "item_name": "文件"}
        return self.precheck_order(order_data)
