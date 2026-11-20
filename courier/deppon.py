"""德邦新开放平台客户端。

签名：Base64(MD5(params_json + appkey + timestamp).hexdigest())。
下单固定使用散单上门取件模式 orderType=1，由德邦通知快递员并由快递员打单。
"""
import base64
import hashlib
import json
import random
import re
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

import requests

from .base import CourierClient
from config.settings import settings
from utils.http_session import pooled_post


class DepponCourier(CourierClient):
    name = "德邦快递"
    TRACE_STATUS_LABELS = {
        "GOT": "已揽收",
        "PICKUP": "已揽收",
        "LOAD_SCAN": "运输中",
        "DEPARTURE": "运输中",
        "ARRIVAL": "运输中",
        "UNLOAD_SCAN": "运输中",
        "TRANSFER": "运输中",
        "DELIVERY": "派送中",
        "DELIVERING": "派送中",
        "SIGNED": "已签收",
        "SIGN": "已签收",
        "REJECT": "异常",
        "RETURN": "异常",
        "CANCEL": "已取消",
    }
    ALLOWED_PRODUCTS = {"XJTK", "DJTK", "XJBK", "DJBK", "YTYDS", "JZZC"}
    QUOTE_PRODUCT_MAP = {
        "TZKJC": "XJTK", "XJTK": "XJTK", "XJBK": "XJBK",
        "HKDJC": "DJTK", "HKDJG": "DJTK", "DJTK": "DJTK", "DJBK": "DJBK",
    }
    QUOTE_PRODUCT_PRIORITY = {"HKDJC": 0, "HKDJG": 1}

    def __init__(self, company_code=None, customer_code=None, appkey=None, sign_value=None):
        self.create_order_url = settings.db_create_order_url.strip()
        self.query_order_url = settings.db_query_order_url.strip()
        self.cancel_order_url = settings.db_cancel_order_url.strip()
        self.trace_query_url = settings.db_trace_query_url.strip()
        self.price_query_url = settings.db_price_query_url.strip()
        self.waybill_fee_url = settings.db_waybill_fee_url.strip()
        self.company_code = (company_code if company_code is not None else settings.db_company_code).strip()
        self.customer_code = (customer_code if customer_code is not None else settings.db_customer_code).strip()
        self.appkey = (appkey if appkey is not None else settings.db_appkey).strip()
        self.sign_value = (sign_value if sign_value is not None else settings.db_sign).strip()
        self.timeout = settings.db_api_timeout

    @property
    def is_mock(self) -> bool:
        return settings.db_mock_mode

    def _configuration_error(self, include_sign: bool = False) -> str:
        missing = []
        if not self.company_code:
            missing.append("DB_COMPANY_CODE")
        if not self.appkey:
            missing.append("DB_APPKEY")
        if include_sign and not self.sign_value:
            missing.append("DB_SIGN")
        return "德邦生产配置缺失：" + "、".join(missing) if missing else ""

    def _sign(self, params_json: str, timestamp: str) -> str:
        plain = params_json + self.appkey + timestamp
        md5_hex = hashlib.md5(plain.encode("utf-8")).hexdigest().lower()
        return base64.b64encode(md5_hex.encode("utf-8")).decode("ascii")

    def _request(self, url: str, params_data: dict) -> dict:
        error = self._configuration_error()
        if error:
            return {"_transport_error": error}
        params_json = json.dumps(params_data, ensure_ascii=False, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        form = {
            "companyCode": self.company_code,
            "digest": self._sign(params_json, timestamp),
            "timestamp": timestamp,
            "params": params_json,
        }
        try:
            response = pooled_post(
                url, data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            # 德邦部分正式接口未在 Content-Type 中声明 UTF-8，requests 会
            # 按 ISO-8859-1 解码，导致中文轨迹乱码。优先直接按 UTF-8 解析字节。
            try:
                value = json.loads(response.content.decode("utf-8"))
            except (TypeError, AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                value = response.json()
            if not isinstance(value, dict):
                raise ValueError("德邦返回的不是 JSON 对象")
            return value
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            return {"_transport_error": f"德邦网络或报文错误：{exc}"}

    def build_gateway_preview(self, url: str, params_data: dict) -> dict:
        """Build the signed form without transmitting it to Deppon."""
        params_json = json.dumps(params_data, ensure_ascii=False, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        return {
            "url": url, "method": "POST",
            "contentType": "application/x-www-form-urlencoded;charset=UTF-8",
            "form": {
                "companyCode": self.company_code,
                "digest": self._sign(params_json, timestamp) if self.appkey else "<未配置 appkey>",
                "timestamp": timestamp, "params": params_data,
            },
            "note": "params 实际以紧凑 JSON 字符串放入表单；appkey 只参与签名，不会发送。",
        }

    @staticmethod
    def _business_result(response: dict) -> tuple[bool, str, str]:
        if response.get("_transport_error"):
            return False, response["_transport_error"], "NETWORK"
        value = response.get("result")
        success = value is True or str(value).strip().lower() == "true"
        code = str(response.get("resultCode") or response.get("error_code") or "")
        message = response.get("reason") or response.get("error_message") or response.get("message") or "德邦业务处理失败"
        return success, message, code

    @staticmethod
    def _required(d: dict, *names: str) -> str:
        missing = [name for name in names if not str(d.get(name) or "").strip()]
        return "下单信息缺失：" + "、".join(missing) if missing else ""

    def _logistic_id(self, order_no: str) -> str:
        value = f"{self.sign_value}{order_no}"
        if len(value) > 32:
            value = f"{self.sign_value}{hashlib.sha256(order_no.encode('utf-8')).hexdigest()[:24]}"
        return value[:32]

    def _build_order_params(self, d: dict) -> dict:
        config_error = self._configuration_error(include_sign=True)
        if config_error:
            raise ValueError(config_error)
        error = self._required(
            d, "order_no", "sender_name", "sender_phone", "sender_province", "sender_city",
            "sender_district", "sender_address", "receiver_name", "receiver_phone",
            "receiver_province", "receiver_city", "receiver_district", "receiver_address", "item_name",
        )
        if error:
            raise ValueError(error)
        customer_code = str(d.get("customer_code") or self.customer_code or "").strip()
        if not customer_code:
            raise ValueError("德邦月结下单缺少 DB_CUSTOMER_CODE")
        product = str(d.get("express_type") or "XJBK").upper().strip()
        if product not in self.ALLOWED_PRODUCTS:
            raise ValueError(f"德邦产品代码无效：{product}；请从已开通产品中选择")
        parcel_count = max(1, int(d.get("parcel_count") or 1))
        if parcel_count != 1:
            raise ValueError("德邦 orderType=1 散单模式不支持子母件，请拆成多个独立订单")
        weight = round(max(0.01, float(d.get("item_weight") or 1.0)), 2)
        order_no = str(d["order_no"]).strip()

        def contact(prefix: str) -> dict:
            value = {
                "name": str(d[f"{prefix}_name"]).strip(),
                "mobile": str(d[f"{prefix}_phone"]).strip(),
                "province": str(d[f"{prefix}_province"]).strip(),
                "city": str(d[f"{prefix}_city"]).strip(),
                "county": str(d[f"{prefix}_district"]).strip(),
                "address": str(d[f"{prefix}_address"]).strip(),
            }
            company = str(d.get(f"{prefix}_company") or "").strip()
            if company:
                value["companyName"] = company
            return value

        params = {
            "logisticID": self._logistic_id(order_no),
            "custOrderNo": order_no,
            "needTraceInfo": 2,
            "companyCode": self.company_code,
            "orderType": "1",
            "transportType": product,
            "customerCode": customer_code,
            "sender": contact("sender"),
            "receiver": contact("receiver"),
            "packageInfo": {
                "cargoName": str(d["item_name"]).strip(),
                "totalNumber": parcel_count,
                "totalWeight": weight,
                "deliveryType": "3",
            },
            "gmtCommit": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "payType": "2",
        }
        volume_m3 = float(d.get("item_volume") or 0)
        if volume_m3 > 0:
            params["packageInfo"]["totalVolume"] = round(volume_m3, 6)
        insurance_amount = float(d.get("insurance_amount") or 0)
        if insurance_amount > 0:
            params["addServices"] = {
                "insuranceType": "0",
                "insuranceValue": round(insurance_amount, 2),
            }
        if d.get("pickup_start_time"):
            params["sendStartTime"] = str(d["pickup_start_time"])
        if d.get("pickup_end_time"):
            params["sendEndTime"] = str(d["pickup_end_time"])
        remark = str(d.get("item_remark") or "").strip()
        if remark:
            params["remark"] = remark
        return params

    def create_order(self, order_data: dict) -> dict:
        if self.is_mock:
            return self._mock_create(order_data)
        if not settings.db_live_order_enabled:
            return {"success": False, "message": "德邦真实下单未启用：请确认后设置 DB_LIVE_ORDER_ENABLED=true"}
        try:
            params = self._build_order_params(order_data)
        except (TypeError, ValueError) as exc:
            return {"success": False, "message": str(exc)}
        response = self._request(self.create_order_url, params)
        ok, message, code = self._business_result(response)
        if not ok:
            return {"success": False, "message": message, "error_code": code, "raw_response": response}
        mail_numbers = [x.strip() for x in str(response.get("mailNo") or "").split(",") if x.strip()]
        primary = response.get("parentMailNo") or (mail_numbers[0] if mail_numbers else "")
        return {
            "success": True,
            "courier_order_no": primary,
            "courier_order_nos": mail_numbers,
            "logistic_id": response.get("logisticID") or params["logisticID"],
            "freight": self._estimate_freight(order_data),
            "product_name": str(order_data.get("express_type_name") or order_data.get("express_type") or "德邦快递"),
            "estimated_delivery": "",
            "message": "德邦已受理，等待快递员接单" + (f"，运单号 {primary}" if primary else ""),
            "raw_response": response,
        }

    def precheck_order(self, order_data: dict) -> dict:
        """在审批前按德邦正式下单报文规则校验，但不发送、不生成运单。"""
        try:
            self._build_order_params(order_data)
        except (TypeError, ValueError) as exc:
            return {"success": False, "message": str(exc), "error_code": "LOCAL_VALIDATION"}
        return {
            "success": True,
            "message": "德邦下单字段校验通过",
            "error_code": "",
            "raw_response": {"validation": "local", "order_created": False},
        }

    def query_order(self, order_no: str = "", courier_order_no: str = "") -> dict:
        if self.is_mock:
            return {"success": True, "message": "Mock query ok", "raw_response": {"mock": True}}
        params = {"logisticCompanyID": "DEPPON"}
        if order_no:
            params["logisticID"] = self._logistic_id(order_no)
        if courier_order_no:
            params["waybillNumber"] = courier_order_no
        response = self._request(self.query_order_url, params)
        ok, message, code = self._business_result(response)
        return {"success": ok, "message": message, "error_code": code, "raw_response": response}

    def query_track(self, courier_order_no: str) -> list:
        if self.is_mock:
            return self._mock_query_track(courier_order_no)
        response = self._request(self.trace_query_url, {"mailNo": courier_order_no})
        ok, _, _ = self._business_result(response)
        if not ok:
            return []
        payload = response.get("responseParam") or response.get("response_param") or response
        candidates = (
            payload.get("trace_list") or payload.get("traceList") or
            payload.get("traces") or payload.get("data") or
            response.get("trace_list") or response.get("traceList") or
            response.get("traces") or response.get("data") or []
        )
        if isinstance(candidates, dict):
            candidates = (
                candidates.get("trace_list") or candidates.get("traceList") or
                candidates.get("traces") or []
            )
        result = []
        for node in candidates if isinstance(candidates, list) else []:
            detail = node.get("desc") or node.get("description") or node.get("remark") or node.get("opRemark") or ""
            op_code = str(node.get("opCode") or node.get("code") or node.get("status") or "").strip()
            status = node.get("opName") or node.get("statusName") or node.get("opStatusName")
            if not status or str(status).strip().upper() == op_code.upper():
                status = self.TRACE_STATUS_LABELS.get(op_code.upper()) or detail or op_code
            result.append({
                "time": (node.get("time") or node.get("acceptTime") or node.get("opTime") or "").replace(" ", "T"),
                "status": status,
                "detail": detail,
                "op_code": op_code,
                "city": node.get("city") or node.get("opAddress") or node.get("address") or "",
                "courier": self._extract_courier(detail),
            })
        return result

    def cancel_order(self, courier_order_no: str, order_no: str = "") -> dict:
        if self.is_mock:
            return {"success": True, "message": "Mock cancel ok"}
        if not settings.db_live_order_enabled:
            return {"success": False, "message": "德邦真实取消未启用：请确认后设置 DB_LIVE_ORDER_ENABLED=true"}
        if not courier_order_no and not order_no:
            return {"success": False, "message": "德邦取消必须提供运单号或原始订单号"}
        params = {
            "logisticCompanyID": "DEPPON",
            "cancelTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "remark": "客户系统撤销订单",
        }
        # 正式取消接口以客户原始 logisticID 定位订单最稳定；运单号刚生成时
        # 可能尚未同步到取消服务，直接传 mailNo 会返回“订单不存在”。
        if order_no:
            params["logisticID"] = self._logistic_id(order_no)
        else:
            params["mailNo"] = courier_order_no
        response = self._request(self.cancel_order_url, params)
        ok, message, code = self._business_result(response)
        if not ok and "已撤销" in str(message):
            ok = True
        return {"success": ok, "message": "德邦订单已撤销" if ok else message,
                "error_code": code, "raw_response": response}

    @staticmethod
    def _price_address(data: dict, prefix: str) -> str:
        province = str(data.get(f"{prefix}_province") or "").strip()
        city = str(data.get(f"{prefix}_city") or "").strip()
        district = str(data.get(f"{prefix}_district") or "").strip()
        detail = str(data.get(f"{prefix}_address") or "").strip()
        if not all((province, city, district, detail)):
            raise ValueError("德邦估价缺少完整寄收件地址")
        return f"{province}-{city}-{district},{detail}"

    def _build_price_query_params(self, order_data: dict) -> dict:
        weight = round(max(0.01, float(order_data.get("weight") or order_data.get("item_weight") or 1)), 2)
        volume = float(order_data.get("volume") or order_data.get("item_volume") or 0)
        # The selected handover time is part of the ETA estimate.  If the
        # carrier cannot quote that slot, the page keeps the product selectable
        # with an unknown ETA instead of falsely marking the route unavailable.
        raw_send_time = order_data.get("pickup_start_time")
        if raw_send_time:
            try:
                send_time = datetime.fromisoformat(
                    str(raw_send_time).replace("Z", "+00:00")
                ).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                send_time = str(raw_send_time).replace("T", " ")[:19]
        else:
            send_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return {
            "logisticCompanyID": "DEPPON",
            "originalsaddress": self._price_address(order_data, "sender"),
            "originalsStreet": self._price_address(order_data, "receiver"),
            "sendDateTime": str(send_time).replace("T", " ")[:19],
            "totalVolume": round(volume, 6) if volume > 0 else 0.001,
            "totalWeight": weight,
            "receiveMethod": "DELIVER_NOUP_AIR",
            "insuredAmount": round(max(0, float(order_data.get("insurance_amount") or 0)), 2),
        }

    @classmethod
    def _map_quote_product(cls, code: str, name: str) -> str:
        code = str(code or "").upper().strip()
        if code in cls.QUOTE_PRODUCT_MAP:
            return cls.QUOTE_PRODUCT_MAP[code]
        name = str(name or "")
        if "标快" in name and ("重包" in name or "大件" in name):
            return "DJBK"
        if "标快" in name:
            return "XJBK"
        if "大件" in name or "重包" in name:
            return "DJTK"
        if "特快" in name or "专递" in name:
            return "XJTK"
        return ""

    @staticmethod
    def _normalize_delivery_time(value, reference_time=None) -> str:
        """Return Deppon ETA using the same absolute date format as SF."""
        text = str(value or "").strip()
        if not text:
            return ""

        absolute = re.search(
            r"(?P<year>\d{4})[-/年](?P<month>\d{1,2})[-/月](?P<day>\d{1,2})日?"
            r"(?:[ T]*(?P<hour>\d{1,2})(?::|点)(?P<minute>\d{1,2})?)?",
            text,
        )
        if absolute:
            parts = absolute.groupdict()
            value = datetime(
                int(parts["year"]), int(parts["month"]), int(parts["day"]),
                int(parts["hour"] or 0), int(parts["minute"] or 0),
            )
            return value.strftime("%Y-%m-%d %H:%M:%S" if parts["hour"] is not None else "%Y-%m-%d")

        try:
            base_time = (
                reference_time if isinstance(reference_time, datetime)
                else datetime.fromisoformat(str(reference_time or "").replace("Z", "+00:00"))
            )
        except (TypeError, ValueError):
            base_time = datetime.now()

        relative_days = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2, "后日": 2}
        offset = next((days for label, days in relative_days.items() if label in text), None)
        if offset is None:
            return text
        target = (base_time + timedelta(days=offset)).date()
        time_value = re.search(r"(?P<hour>\d{1,2})(?::|点)(?P<minute>\d{1,2})?", text)
        if not time_value:
            return target.strftime("%Y-%m-%d")
        return datetime(
            target.year, target.month, target.day,
            int(time_value.group("hour")), int(time_value.group("minute") or 0),
        ).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _quote_product_groups(_weight: float) -> list[tuple[str, ...]]:
        # 重量和体积只用于推荐；是否可选以德邦对各产品返回的实时报价为准。
        # 大件特快优先查询次日达，当前线路不可达时再查询隔日达。
        return [
            ("TZKJC",),
            ("XJBK",),
            ("HKDJC", "HKDJG"),
            ("DJBK",),
        ]

    @staticmethod
    def _extract_quote_fee(row) -> float | None:
        raw_fee = next((row.get(key) for key in (
            "totalfee", "totalFee", "totalAmount", "fee", "price"
        ) if row.get(key) not in (None, "")), None)
        try:
            fee = float(raw_fee) if raw_fee is not None else None
        except (TypeError, ValueError):
            return None
        # 价格接口有时会用 0 表示该线路/产品没有可用价格，不代表免费。
        return fee if fee is not None and fee > 0 else None

    def query_delivery_quotes(self, order_data: dict) -> dict:
        if self.is_mock:
            try:
                quote_time = datetime.fromisoformat(
                    str(order_data.get("pickup_start_time") or "").replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except (TypeError, ValueError):
                quote_time = datetime.now()
            delivery = (quote_time + timedelta(days=2)).strftime("%Y-%m-%d 18:00:00")
            return {
                "success": True, "message": "Mock 报价成功", "items": [
                    {"business_type": "XJTK", "business_type_desc": "小件特快（Mock）",
                     "estimated_fee": 12.5, "estimated_delivery": delivery, "currency": "CNY"},
                    {"business_type": "XJBK", "business_type_desc": "小件标快（Mock）",
                     "estimated_fee": 10.0, "estimated_delivery": delivery, "currency": "CNY"},
                    {"business_type": "DJTK", "business_type_desc": "大件特快（Mock）",
                     "estimated_fee": 28.0, "estimated_delivery": delivery, "currency": "CNY"},
                    {"business_type": "DJBK", "business_type_desc": "大件标快（Mock）",
                     "estimated_fee": 24.0, "estimated_delivery": delivery, "currency": "CNY"},
                ], "request": {}, "raw_response": {"mock": True},
            }
        try:
            base_payload = self._build_price_query_params(order_data)
        except (TypeError, ValueError) as exc:
            return {"success": False, "message": str(exc), "items": [], "request": {}}

        def query_product_group(candidates):
            group_rows = []
            group_requests = []
            group_raw_queries = []
            group_errors = []
            group_success = False
            fallback_rows = []
            for product_code in candidates:
                payload = dict(base_payload)
                payload["productCode"] = product_code
                group_requests.append(payload)
                response = self._request(self.price_query_url, payload)
                group_raw_queries.append({"productCode": product_code, "response": response})
                ok, message, code = self._business_result(response)
                if not ok:
                    group_errors.append({"productCode": product_code, "message": message, "code": code})
                    continue
                group_success = True
                response_rows = response.get("responseParam") or []
                if isinstance(response_rows, dict):
                    response_rows = [response_rows]
                if not isinstance(response_rows, list) or not response_rows:
                    continue
                matching_rows = [
                    row for row in response_rows
                    if str(row.get("producteCode") or row.get("productCode") or "").upper() == product_code
                ]
                candidate_rows = matching_rows or response_rows
                fallback_rows = candidate_rows
                if any(self._extract_quote_fee(row) is not None for row in candidate_rows):
                    group_rows.extend(candidate_rows)
                    break
                group_errors.append({
                    "productCode": product_code,
                    "message": "德邦接口未返回有效费用",
                    "code": "EMPTY_PRICE",
                })
            else:
                # 同组备用编码都没有有效报价时，保留最后一次产品信息用于展示。
                group_rows.extend(fallback_rows)
            return group_rows, group_requests, group_raw_queries, group_errors, group_success

        groups = self._quote_product_groups(float(base_payload["totalWeight"]))
        # 四种产品互不依赖，一次并行查询。每组内部仍按顺序尝试备用编码，
        # 既保留全部产品的可选能力，也避免两批串行查询拖慢寄件页。
        with ThreadPoolExecutor(max_workers=len(groups)) as executor:
            group_results = list(executor.map(query_product_group, groups))

        rows = []
        requests_made = []
        raw_queries = []
        errors = []
        any_success = False
        for group_rows, group_requests, group_raw, group_errors, group_success in group_results:
            rows.extend(group_rows)
            requests_made.extend(group_requests)
            raw_queries.extend(group_raw)
            errors.extend(group_errors)
            any_success = any_success or group_success

        selected = {}
        for row in rows:
            quote_code = str(row.get("producteCode") or row.get("productCode") or "").upper()
            business_type = self._map_quote_product(quote_code, row.get("productName"))
            if not business_type:
                continue
            fee = self._extract_quote_fee(row)
            item = {
                "business_type": business_type,
                "business_type_desc": str(row.get("productName") or ""),
                "estimated_fee": fee,
                "estimated_delivery": self._normalize_delivery_time(
                    row.get("arriveDate") or row.get("days"),
                    base_payload.get("sendDateTime"),
                ),
                "currency": "CNY", "source_product_code": quote_code,
            }
            priority = self.QUOTE_PRODUCT_PRIORITY.get(quote_code, 0)
            current = selected.get(business_type)
            if current is None or priority < current[0]:
                selected[business_type] = (priority, item)

        if not selected:
            first_error = errors[0] if errors else {}
            return {
                "success": False,
                "message": first_error.get("message") or "德邦本次未返回有效价格和时效",
                "error_code": first_error.get("code") or "",
                "items": [], "request": base_payload, "requests": requests_made,
                "raw_response": {"queries": raw_queries},
            }
        return {
            "success": True, "message": "德邦价格时效查询成功",
            "items": [value[1] for value in selected.values()],
            "request": base_payload, "requests": requests_made,
            "errors": errors, "raw_response": {"queries": raw_queries},
        }

    def query_actual_freight(self, courier_order_no: str) -> dict:
        if self.is_mock:
            return {"success": False, "message": "Mock 模式没有真实运费"}
        if not courier_order_no:
            return {"success": False, "message": "德邦运费查询缺少运单号"}
        payload = {"mailNo": courier_order_no}
        if self.customer_code:
            payload["customerCode"] = self.customer_code
        response = self._request(self.waybill_fee_url, payload)
        ok, message, code = self._business_result(response)
        if not ok:
            return {"success": False, "message": message, "error_code": code,
                    "request": payload, "raw_response": response}
        detail = response.get("responseParam") or {}
        if not isinstance(detail, dict):
            detail = {}
        value = detail.get("totalPrice")
        try:
            freight = float(value) if value is not None and str(value).strip() else None
        except (TypeError, ValueError):
            freight = None
        return {"success": True, "freight": freight, "fee_detail": detail,
                "message": "德邦运费查询成功" if freight is not None else "德邦尚未返回运费",
                "request": payload, "raw_response": response}

    def query_fee(self, sender: dict, receiver: dict, weight: float) -> dict:
        return {"success": False, "freight": None,
                "message": "未取得德邦月结线路报价，不能使用固定首续重伪造价格"}

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
        waybill = "DPKMOCK" + now.strftime("%Y%m%d%H%M%S") + str(random.randint(100, 999))
        return {
            "success": True, "courier_order_no": waybill, "courier_order_nos": [waybill],
            "freight": self._estimate_freight(d), "product_name": "德邦小件标快（Mock）",
            "estimated_delivery": (now + timedelta(days=2)).strftime("%Y-%m-%d 18:00:00"),
            "message": "Mock 下单成功，不会通知真实快递员", "raw_response": {"mock": True},
        }

    @staticmethod
    def _mock_query_track(_: str) -> list:
        now = datetime.now()
        return [{"time": now.isoformat(timespec="seconds"), "status": "Mock 已下单",
                 "detail": "模拟轨迹，不是真实德邦状态", "city": "",
                 "courier": {"name": "", "phone": ""}}]
