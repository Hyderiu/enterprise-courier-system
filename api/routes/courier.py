from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator
from typing import Optional, List
from database.connection import get_db
from database import models
import datetime as dt
import base64
import copy
import hashlib
import hmac
import json
import logging
from pathlib import Path
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import Future, ThreadPoolExecutor

router = APIRouter()
log = logging.getLogger("courier.routes")

_quote_cache_lock = threading.Lock()
_quote_cache = {}
_quote_inflight = {}
COURIER_INFO_NAME = {"SF": "顺丰", "DB": "德邦", "JDL": "京东"}


@router.get("/status")
def carrier_status(region: str = "xiamen_other", db: Session = Depends(get_db)):
    """只返回配置完整性，不泄露客户编码、月结号或签名密钥。"""
    from config.settings import settings

    from utils.regions import enabled_carriers, get_config, normalize_region
    region = normalize_region(region)

    def db_cfg(key: str, fallback: str = "") -> str:
        return get_config(db, region, key, "")

    sf_customer_code = db_cfg("sf_customer_code", settings.sf_customer_code)
    sf_check_word = db_cfg("sf_check_word", settings.sf_check_word)
    sf_monthly_card = db_cfg("sf_monthly_card", settings.sf_monthly_card)
    sf_city_dev_id = db_cfg("sf_city_dev_id") or settings.sf_city_dev_id
    sf_city_dev_key = db_cfg("sf_city_dev_key") or settings.sf_city_dev_key
    sf_city_shop_id = db_cfg("sf_city_shop_id") or settings.sf_city_shop_id
    sf_city_live = (db_cfg(
        "sf_city_live_order_enabled",
        "1" if settings.sf_city_live_order_enabled else "0",
    ) or ("1" if settings.sf_city_live_order_enabled else "0")).lower() in {
        "1", "true", "yes", "on"
    }
    db_company_code = db_cfg("db_company_code", settings.db_company_code)
    db_customer_code = db_cfg("db_customer_code", settings.db_customer_code)
    db_appkey = db_cfg("db_appkey", settings.db_appkey)
    db_sign = db_cfg("db_sign", settings.db_sign)
    jdl_app_key = db_cfg("jdl_app_key", settings.jdl_app_key)
    jdl_app_secret = db_cfg("jdl_app_secret", settings.jdl_app_secret)
    jdl_access_token = db_cfg("jdl_access_token", settings.jdl_access_token)
    jdl_customer_code = db_cfg("jdl_customer_code", settings.jdl_customer_code)
    jdl_business_unit_code = db_cfg(
        "jdl_business_unit_code", settings.jdl_business_unit_code
    )
    version_path = Path(__file__).resolve().parents[2] / "VERSION"
    version = version_path.read_text(encoding="utf-8-sig").strip() if version_path.is_file() else "unknown"
    return {
        "ok": True,
        "region_code": region,
        "enabled_carriers": enabled_carriers(db, region),
        "version": version,
        "sf": {
            "mock_mode": settings.sf_mock_mode,
            "live_order_enabled": settings.sf_live_order_enabled,
            "credentials_configured": bool(sf_customer_code and sf_check_word),
            "billing_configured": bool(sf_monthly_card),
            "ready_for_precheck": bool(sf_customer_code and sf_check_word),
            "ready_for_live_order": bool(
                not settings.sf_mock_mode and settings.sf_live_order_enabled
                and sf_customer_code and sf_check_word
            ),
        },
        "sf_city": {
            "credentials_configured": bool(sf_city_dev_id and sf_city_dev_key and sf_city_shop_id),
            "billing_configured": bool(sf_city_shop_id),
            "ready_for_precheck": bool(sf_city_dev_id and sf_city_dev_key and sf_city_shop_id),
            "ready_for_live_order": bool(
                sf_city_live and sf_city_dev_id and sf_city_dev_key and sf_city_shop_id
            ),
        },
        "deppon": {
            "mock_mode": settings.db_mock_mode,
            "live_order_enabled": settings.db_live_order_enabled,
            "credentials_configured": bool(
                db_company_code and db_customer_code and db_appkey and db_sign
            ),
            "billing_configured": bool(db_customer_code),
            "ready_for_live_order": bool(
                not settings.db_mock_mode and settings.db_live_order_enabled
                and db_company_code and db_customer_code and db_appkey and db_sign
            ),
        },
        "jdl": {
            "live_order_enabled": settings.jdl_live_order_enabled,
            "credentials_configured": bool(
                jdl_app_key and jdl_app_secret and jdl_access_token and jdl_customer_code
            ),
            "billing_configured": bool(jdl_customer_code),
            "freight_configured": bool(jdl_business_unit_code),
            "ready_for_precheck": bool(
                jdl_app_key and jdl_app_secret and jdl_access_token and jdl_customer_code
            ),
            "ready_for_live_order": bool(
                settings.jdl_live_order_enabled
                and jdl_app_key and jdl_app_secret and jdl_access_token and jdl_customer_code
            ),
        },
    }


class FeeQuery(BaseModel):
    courier_type: str
    sender_province: Optional[str] = None
    sender_city: Optional[str] = None
    receiver_province: str
    receiver_city: str
    weight: Optional[float] = None


class QuoteQuery(BaseModel):
    sender_province: Optional[str] = None
    sender_city: Optional[str] = None
    sender_district: Optional[str] = None
    sender_address: Optional[str] = None
    receiver_province: Optional[str] = None
    receiver_city: Optional[str] = None
    receiver_district: Optional[str] = None
    receiver_address: Optional[str] = None
    item_type: Optional[str] = None
    weight: Optional[float] = None
    volume: Optional[float] = None
    parcel_count: int = 1
    insurance_amount: float = 0
    pickup_start_time: Optional[str] = None
    pickup_end_time: Optional[str] = None
    same_day_required: bool = False


def _money_amount(value):
    try:
        return round(float(value), 2) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _base_and_insurance(total, insurance):
    """Split a carrier total into base freight and insurance without double counting."""
    total_amount = _money_amount(total)
    insurance_amount = _money_amount(insurance)
    if total_amount is None:
        return None, insurance_amount, None
    if insurance_amount is None:
        return total_amount, None, None
    return round(max(0, total_amount - insurance_amount), 2), insurance_amount, total_amount


def _estimated_sf_insurance(declared_value: float) -> float:
    value = max(0, float(declared_value or 0))
    if value <= 0:
        return 0.0
    return round(1 if value <= 500 else 2 if value <= 1000 else value * 0.005, 2)


def _explicitly_unavailable(message: str = "", code: str = "") -> bool:
    """True only when the carrier explicitly rejects the route/product.

    Missing fields in an advisory quote, a timeout, an empty response, or a
    transient network failure are not proof that the product cannot be used.
    """
    code_text = str(code or "").upper().strip()
    text = str(message or "").strip().lower()
    if code_text in {"CONFIG", "CONFIGURATION", "LOCAL_VALIDATION"}:
        return True
    transient = (
        "network", "timeout", "超时", "网络", "无响应", "未返回",
        "查询失败", "报文错误", "empty_price", "未返回有效费用",
    )
    if any(marker in text or marker in code_text.lower() for marker in transient):
        return False
    explicit = (
        "不支持", "不可达", "不在服务范围", "超出服务范围", "超区",
        "无可用产品", "适用产品不满足", "线路不存在", "无法寄递",
    )
    return any(marker in text for marker in explicit)


def _quote_services(
    q: QuoteQuery, db: Session, region_code: str,
    carrier_filter: str | None = None,
    service_filter: str | None = None,
):
    """Return product recommendations and live SF fee/ETA estimates."""
    from courier_services import (
        ALL_SERVICES, SERVICE_MAP, available_in_region,
        service_display_name,
    )
    from utils.regions import (
        DEFAULT_RECOMMENDED_SERVICES, default_service_id, enabled_carriers, normalize_region,
    )
    allowed_carriers = set(enabled_carriers(db, region_code)) if db is not None else {"SF", "DB"}
    if carrier_filter:
        allowed_carriers &= {str(carrier_filter).upper()}
    region_services = [
        service for service in ALL_SERVICES
        if service["courier"] in allowed_carriers
        and available_in_region(service, region_code)
        and (not service_filter or service["id"] == service_filter)
    ]

    recommended_id = (
        default_service_id(db, region_code) if db is not None
        else DEFAULT_RECOMMENDED_SERVICES.get(normalize_region(region_code))
    )
    recommended_service = SERVICE_MAP.get(recommended_id or "")
    reason = (
        f"系统默认推荐{service_display_name(recommended_service)}，您也可以自行选择其他快递产品。"
        if recommended_service else "当前地区尚未设置默认推荐产品。"
    )
    route_complete = all((
        q.sender_province, q.sender_city, q.sender_district, q.sender_address,
        q.receiver_province, q.receiver_city, q.receiver_district, q.receiver_address,
    ))
    weight_complete = q.weight is not None and q.weight > 0
    quote_ready = route_complete and weight_complete
    carrier_quotes = {"SF": {}, "DB": {}, "JDL": {}}
    carrier_errors = {"SF": "", "DB": "", "JDL": ""}
    product_errors = {"SF": {}, "DB": {}, "JDL": {}}
    if quote_ready:
        from courier import get_courier
        from config.settings import settings
        clients, futures, jdl_services, sf_services = {}, {}, [], []
        quote_started = time.perf_counter()
        def prepare_client(client):
            # Do not shorten the carrier's own network timeout for page quotes.
            # A slow but valid response is preferable to falsely disabling all
            # products. Each adapter still retains its native network timeout.
            return client

        def measured_call(courier_type, operation, func, *args):
            started = time.perf_counter()
            try:
                return func(*args)
            finally:
                elapsed_ms = round((time.perf_counter() - started) * 1000)
                log.info(
                    "courier_quote carrier=%s operation=%s elapsed_ms=%s region=%s",
                    courier_type, operation, elapsed_ms, region_code,
                )

        def resolve(future, fallback):
            try:
                return future.result()
            except Exception as exc:
                return {"success": False, "items": [], "message": f"{fallback}：{exc}"}

        # Carrier APIs are independent. Query them concurrently so one slow
        # provider no longer serially blocks every product card on the page.
        pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="courier-quote")
        try:
            for courier_type, label in (("SF", "顺丰"), ("DB", "德邦")):
                if courier_type not in allowed_carriers:
                    continue
                client = (
                    get_courier(courier_type, db, region_code)
                    if db is not None else get_courier(courier_type, db)
                )
                client = prepare_client(client)
                clients[courier_type] = client
                quote_data = q.model_dump()
                if courier_type == "SF":
                    # Query the aggregate route and configured products in the
                    # same round.  The aggregate response sometimes has data
                    # that a product-specific query omits (and vice versa), so
                    # merge both while avoiding the old serial fallback wait.
                    sf_services = [
                        row for row in region_services if row["courier"] == "SF"
                    ]
                    futures[("SF", "base")] = pool.submit(
                        measured_call, "SF", "base",
                        client.query_delivery_quotes, quote_data,
                    )
                    for service in sf_services:
                        product_data = dict(quote_data)
                        product_data["business_type"] = service["express_type"]
                        futures[("SF", service["express_type"])] = pool.submit(
                            measured_call, "SF", service["express_type"],
                            client.query_delivery_quotes, product_data,
                        )
                elif courier_type == "DB" and q.insurance_amount > 0:
                    base_data = dict(quote_data)
                    base_data["insurance_amount"] = 0
                    futures[(courier_type, "base")] = pool.submit(
                        measured_call, courier_type, "base",
                        client.query_delivery_quotes, base_data
                    )
                    futures[(courier_type, "insured")] = pool.submit(
                        measured_call, courier_type, "insured",
                        client.query_delivery_quotes, quote_data
                    )
                else:
                    futures[(courier_type, "base")] = pool.submit(
                        measured_call, courier_type, "base",
                        client.query_delivery_quotes, quote_data
                    )
            if "JDL" in allowed_carriers:
                client = get_courier("JDL", db, region_code)
                client = prepare_client(client)
                clients["JDL"] = client
                jdl_services = [row for row in region_services if row["courier"] == "JDL"]
                for service in jdl_services:
                    futures[("JDL", service["express_type"])] = pool.submit(
                        measured_call, "JDL", service["express_type"],
                        client.precheck_order, {
                    "sender_province": q.sender_province, "sender_city": q.sender_city,
                    "sender_district": q.sender_district, "sender_address": q.sender_address,
                    "sender_name": "企业员工", "sender_phone": "13800000000",
                    "receiver_province": q.receiver_province, "receiver_city": q.receiver_city,
                    "receiver_district": q.receiver_district, "receiver_address": q.receiver_address,
                    "receiver_name": "收件人", "receiver_phone": "13800000000",
                    "item_name": q.item_type or "文件", "item_weight": q.weight,
                    "item_volume": q.volume, "item_count": 1,
                    "parcel_count": q.parcel_count,
                    "insurance_amount": q.insurance_amount,
                    "pickup_start_time": q.pickup_start_time,
                    "pickup_end_time": q.pickup_end_time,
                        }, service["express_type"]
                    )
        finally:
            # 浏览器即使取消请求，承运商网络调用也无法强行中断。这里不等待
            # 已超时任务，避免一个慢接口长期占住当前 API 请求线程。
            pool.shutdown(wait=False, cancel_futures=False)

        if sf_services:
            aggregate = resolve(futures[("SF", "base")], "顺丰线路查询失败")
            sf_items = list(aggregate.get("items", [])) if aggregate.get("success") else []
            returned = {
                str(item.get("business_type") or ""): item for item in sf_items
            }
            sf_errors = []
            for service in sf_services:
                express_type = service["express_type"]
                result = resolve(
                    futures[("SF", express_type)], f"顺丰{service['name']}查询失败"
                )
                matched = [
                    item for item in result.get("items", [])
                    if str(item.get("business_type") or "") == express_type
                ] if result.get("success") else []
                if matched:
                    targeted_item = matched[0]
                    existing = returned.get(express_type)
                    if existing:
                        for field in (
                            "business_type_desc", "estimated_fee",
                            "estimated_delivery", "currency",
                        ):
                            if not existing.get(field) and targeted_item.get(field) is not None:
                                existing[field] = targeted_item[field]
                    else:
                        sf_items.append(targeted_item)
                        returned[express_type] = targeted_item
                else:
                    message = result.get("message") or "顺丰接口本次未返回该产品的报价或时效"
                    product_errors["SF"][express_type] = message
                    sf_errors.append(message)
            carrier_quotes["SF"] = {
                item["business_type"]: item for item in sf_items
            }
            if not sf_items:
                base_error = aggregate.get("message") if not aggregate.get("success") else ""
                carrier_errors["SF"] = "；".join(dict.fromkeys(
                    [message for message in [base_error, *sf_errors] if message]
                ))

        for courier_type, label in (("DB", "德邦"),):
            if (courier_type, "base") not in futures:
                continue
            result = resolve(futures[(courier_type, "base")], f"{label}查询失败")
            if courier_type == "DB" and (courier_type, "insured") in futures:
                insured_result = resolve(
                    futures[(courier_type, "insured")], "德邦含保价查询失败"
                )
                insured_items = {
                    str(item.get("business_type") or ""): item
                    for item in insured_result.get("items", [])
                } if insured_result.get("success") else {}
                for item in result.get("items", []):
                    insured_item = insured_items.get(str(item.get("business_type") or ""))
                    base_fee = _money_amount(item.get("estimated_fee"))
                    insured_total = _money_amount(
                        insured_item.get("estimated_fee") if insured_item else None
                    )
                    item["insurance_fee"] = (
                        round(max(0, insured_total - base_fee), 2)
                        if base_fee is not None and insured_total is not None else None
                    )
                    item["estimated_total_fee"] = insured_total
            carrier_quotes[courier_type] = {
                item["business_type"]: item for item in result.get("items", [])
            }
            for error in result.get("errors", []):
                product_code = str(error.get("productCode") or "")
                business_type = clients["DB"]._map_quote_product(product_code, "")
                if business_type:
                    product_errors["DB"][business_type] = error.get("message") or ""
            if not result.get("success"):
                carrier_errors[courier_type] = result.get("message") or f"{label}时效价格查询失败"

        if jdl_services:
            client = clients["JDL"]
            for service in jdl_services:
                result = resolve(
                    futures[("JDL", service["express_type"])], "京东时效价格查询失败"
                )
                data = result.get("data") if isinstance(result.get("data"), dict) else {}
                fee_info = data.get("commonFeeInfoResponse") or {}
                fee_details = fee_info.get("feeDetailInfoResponseList") or []
                insurance_code = (
                    client.FREIGHT_INSURANCE_PRODUCT_CODE
                    if service["express_type"].startswith("fr-")
                    else client.EXPRESS_INSURANCE_PRODUCT_CODE
                )
                insurance_fee = sum(
                    amount for amount in (
                        _money_amount(detail.get("amount"))
                        for detail in fee_details
                        if detail.get("productCode") == insurance_code
                    ) if amount is not None
                )
                if q.insurance_amount <= 0:
                    insurance_fee = 0.0
                elif not any(
                    detail.get("productCode") == insurance_code for detail in fee_details
                ):
                    insurance_fee = None
                base_fee, insurance_fee, total_fee = _base_and_insurance(
                    fee_info.get("totalAmount"), insurance_fee
                )
                product_info = next((
                    item for item in (data.get("productInfos") or [])
                    if item.get("productCode") == service["express_type"]
                ), {})
                delivery_millis = product_info.get("deliveryPromiseTime")
                delivery = None
                if delivery_millis:
                    try:
                        delivery = dt.datetime.fromtimestamp(
                            int(delivery_millis) / 1000
                        ).strftime("%Y-%m-%d %H:%M")
                    except (TypeError, ValueError, OSError):
                        delivery = None
                carrier_quotes["JDL"][service["express_type"]] = {
                    "available": bool(result.get("success")),
                    "message": result.get("message") or "",
                    "error_code": result.get("error_code") or "",
                    "estimated_fee": base_fee,
                    "estimated_total_fee": total_fee,
                    "estimated_delivery": delivery,
                    "insurance_fee": insurance_fee,
                }

    items = []
    for service in region_services:
        fee = delivery = insurance_fee = total_fee = None
        quote_available = False
        quote_deferred = False
        if not route_complete:
            quote_note = "请完整填写寄件和收件省市区及详细地址"
        elif not weight_complete:
            quote_note = "请先填写大于0kg的预估重量"
        else:
            courier_type = service["courier"]
            quote = carrier_quotes[courier_type].get(service["express_type"])
            if not quote:
                failure_message = (
                    product_errors[courier_type].get(service["express_type"])
                    or carrier_errors[courier_type]
                )
                if _explicitly_unavailable(failure_message):
                    quote_note = failure_message or "承运商明确返回当前线路不支持该产品"
                else:
                    quote_available = True
                    quote_deferred = True
                    quote_note = (
                        f"{COURIER_INFO_NAME.get(courier_type, courier_type)}实时查询暂未返回，"
                        "仍可选择；提交时将再次校验，费用和时效以承运商受理为准"
                    )
            else:
                fee = quote.get("estimated_fee")
                delivery = quote.get("estimated_delivery") or None
                insurance_fee = quote.get("insurance_fee")
                total_fee = quote.get("estimated_total_fee")
                if courier_type == "JDL":
                    insurance_ready = q.insurance_amount <= 0 or insurance_fee is not None
                    explicit_unavailable = _explicitly_unavailable(
                        quote.get("message"), quote.get("error_code")
                    )
                    quote_available = not explicit_unavailable
                    quote_deferred = not bool(quote.get("available")) or not insurance_ready
                    quote_note = (
                        "当前线路支持该产品"
                        if quote.get("available") and insurance_ready
                        else (quote.get("message") or "京东实时查询暂未返回，提交时将再次校验")
                    )
                    items.append({
                        "id": service["id"], "estimated_fee": fee,
                        "estimated_total_fee": total_fee,
                        "estimated_delivery": delivery, "quote_available": quote_available,
                        "quote_note": quote_note,
                        "insurance_fee": quote.get("insurance_fee"),
                    })
                    continue
                # 顺丰即日可能在晚间仍返回可下单价格，但不返回具体承诺时刻。
                # 价格已由顺丰按指定产品返回时允许选择，并明确提示时效缺失；
                # 其他产品仍要求价格和时效同时完整，避免把不完整报价误判为可用。
                sf_same_day_without_eta = (
                    courier_type == "SF"
                    and service["express_type"] == "6"
                    and fee is not None
                    and not delivery
                )
                quote_available = True
                quote_deferred = not ((fee is not None and bool(delivery)) or sf_same_day_without_eta)
                if quote_deferred:
                    quote_note = "承运商已返回该产品，费用或时效待补充；最终以正式受理为准"
                elif sf_same_day_without_eta:
                    quote_note = "当前线路可寄；具体送达时间以快递公司受理为准"
                else:
                    quote_note = "当前线路可寄，费用和时效已更新"
                    if courier_type == "SF" and q.pickup_start_time:
                        quote_note = "当前线路可预约；页面时效为线路参考，最终以预约取件后顺丰受理为准"
                if courier_type == "SF" and q.insurance_amount > 0 and fee is not None:
                    insurance_fee = _estimated_sf_insurance(q.insurance_amount)
                    total_fee = round(float(fee) + insurance_fee, 2)
                if courier_type == "DB" and q.insurance_amount > 0:
                    insurance_ready = insurance_fee is not None and total_fee is not None
                    if quote_available and not insurance_ready:
                        quote_deferred = True
                        quote_note = "德邦本次未返回完整保价费用，仍可提交；最终以月结账单为准"
        items.append({
            "id": service["id"], "estimated_fee": fee,
            "estimated_total_fee": total_fee,
            "estimated_delivery": delivery, "quote_available": quote_available,
            "quote_note": quote_note,
            "quote_deferred": quote_deferred,
            "insurance_fee": insurance_fee,
        })

    result = {
        "ok": True, "recommended_id": recommended_id,
        "recommendation_reason": reason, "items": items,
        "requirements": {
            "SF": {"volume_required": False},
            "DB": {"volume_required": False, "default_volume": 0.001},
        },
    }
    if quote_ready:
        elapsed_ms = round((time.perf_counter() - quote_started) * 1000)
        result["elapsed_ms"] = elapsed_ms
        result["carrier"] = carrier_filter or "ALL"
        log.info(
            "courier_quote_complete carrier=%s elapsed_ms=%s items=%s region=%s",
            carrier_filter or "ALL", elapsed_ms, len(items), region_code,
        )
    return result


def _quote_cache_key(q: QuoteQuery, region_code: str, carrier: str | None) -> str:
    payload = q.model_dump(mode="json")
    return json.dumps(
        {"region": region_code, "carrier": carrier or "ALL", "query": payload},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _cached_quote_services(
    q: QuoteQuery, db: Session, region_code: str, carrier: str | None,
    force_refresh: bool = False,
):
    from config.settings import settings

    key = _quote_cache_key(q, region_code, carrier)
    now = time.monotonic()
    owner = False
    with _quote_cache_lock:
        cached = _quote_cache.get(key)
        cache_seconds = max(300, int(settings.courier_quote_cache_seconds))
        if not force_refresh and cached and now - cached[0] <= cache_seconds:
            result = copy.deepcopy(cached[1])
            result["cache_hit"] = True
            return result
        future = _quote_inflight.get(key)
        if future is None:
            future = Future()
            _quote_inflight[key] = future
            owner = True
    if not owner:
        result = copy.deepcopy(future.result())
        result["shared_inflight"] = True
        return result
    try:
        result = _quote_services(q, db, region_code, carrier)
        has_live_quote = any(
            item.get("quote_available") and not item.get("quote_deferred")
            for item in result.get("items", [])
        )
        with _quote_cache_lock:
            if has_live_quote:
                _quote_cache[key] = (time.monotonic(), copy.deepcopy(result))
            elif cached:
                stale = copy.deepcopy(cached[1])
                stale["stale_cache"] = True
                for item in stale.get("items", []):
                    if item.get("quote_available"):
                        item["quote_note"] = (
                            "承运商实时查询暂未返回，显示最近一次成功结果；"
                            "提交后仍以承运商最终受理和月结账单为准"
                        )
                result = stale
        future.set_result(copy.deepcopy(result))
        return result
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        with _quote_cache_lock:
            _quote_inflight.pop(key, None)


@router.post("/quotes")
def quote_services_api(
    q: QuoteQuery, request: Request, region: str = None,
    carrier: str = None, force: bool = False,
    db: Session = Depends(get_db),
):
    from utils.regions import request_region
    normalized_carrier = str(carrier or "").upper() or None
    if normalized_carrier not in {None, "SF", "DB", "JDL"}:
        raise HTTPException(400, "不支持的快递方")
    region_code = request_region(
        db, request.state.current_user, region,
        auth_enabled=bool(getattr(request.state, "auth_enabled", False)),
    )
    return _cached_quote_services(
        q, db, region_code, normalized_carrier, force_refresh=force
    )


def quote_services(q: QuoteQuery, db: Session):
    """Backward-compatible service used by unit tests and internal callers."""
    return _quote_services(q, db, "xiamen_other")


def require_selected_service_quote(
    service_id: str, q: QuoteQuery, db: Session, region_code: str = "xiamen_other",
    prefer_live: bool = False,
) -> dict:
    """Recheck the selected product server-side and return its authoritative quote."""
    from config.settings import settings
    from courier_services import SERVICE_MAP

    service = SERVICE_MAP.get(service_id)
    if (
        service and service.get("courier") == "DB"
        and not settings.db_mock_mode and not prefer_live
    ):
        # Formal single-order submission must not be blocked by Deppon's
        # advisory endpoint. Batch review opts into the live result explicitly.
        return {
            "id": service_id,
            "estimated_fee": None,
            "estimated_delivery": None,
            "quote_available": True,
            "quote_deferred": True,
            "quote_note": "德邦费用和时效以正式受理及月结账单为准",
        }
    carrier_filter = service.get("courier") if service else None
    # Only query the selected carrier.  Besides being much faster for batches,
    # this also lets Deppon's live estimate reach the review table instead of
    # unconditionally replacing it with the old "monthly statement" fallback.
    # _quote_services already treats a transient empty carrier response as a
    # deferred (but selectable) quote, so order submission remains resilient.
    result = _quote_services(q, db, region_code, carrier_filter, service_id)
    item = next((row for row in result.get("items", []) if row.get("id") == service_id), None)
    if not item or not item.get("quote_available"):
        note = (item or {}).get("quote_note") or "当前路线不支持该快递产品"
        raise HTTPException(422, f"所选快递产品当前不可用：{note}")
    return item


class SFRequestPreviewQuery(BaseModel):
    courier_type: Optional[str] = None
    sender_name: str
    sender_phone: str
    sender_company: Optional[str] = None
    sender_province: str
    sender_city: str
    sender_district: str
    sender_address: str
    receiver_name: str
    receiver_phone: str
    receiver_company: Optional[str] = None
    receiver_province: str
    receiver_city: str
    receiver_district: str
    receiver_address: str
    item_name: str
    item_weight: float
    item_volume: Optional[float] = None
    insurance_amount: float = 0
    parcel_count: int = 1
    pickup_time_mode: str = "immediate"
    pickup_start_time: Optional[str] = None
    pickup_end_time: Optional[str] = None
    item_remark: Optional[str] = None
    express_type: str = "2"
    debug_region_code: Optional[str] = None


@router.post("/sf/request-preview")
def sf_request_preview(q: SFRequestPreviewQuery, db: Session = Depends(get_db)):
    """Generate signed request previews locally. No carrier API is called."""
    from courier import get_courier

    from utils.regions import normalize_region
    client = get_courier("SF", db, normalize_region(q.debug_region_code))
    order_data = q.model_dump()
    order_data["order_no"] = "CO<提交时由后端生成32位UUID>"
    order_data["weight"] = q.item_weight
    order_data["volume"] = q.item_volume
    try:
        quote_payload = client._build_delivery_quote_payload(order_data)
        pre_payload = client._build_pre_order_payload(order_data)
        create_payload = client._build_create_order_payload(order_data)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc))
    return {
        "ok": True,
        "requests": [
            {"key": "quote", "title": "时效标准及价格查询（自动发送）",
             **client.build_gateway_preview(client.SVC_QUERY_TIMETM, quote_payload)},
            {"key": "pre_order", "title": "预下单校验（提交审批前发送）",
             **client.build_gateway_preview(client.SVC_PRE_ORDER, pre_payload)},
            {"key": "create_order", "title": "正式下单（审批通过后才发送）",
             **client.build_gateway_preview(client.SVC_CREATE_ORDER, create_payload)},
            {"key": "search_order", "title": "订单结果查询（下单返回不确定时）",
             **client.build_gateway_preview(client.SVC_SEARCH_ORDER, {
                 "searchType": "1", "orderId": order_data["order_no"], "language": "zh-CN",
             })},
            {"key": "sub_mailno", "title": "子单号申请（缺少子单号时）",
             **client.build_gateway_preview(client.SVC_GET_SUB_MAILNO, {
                 "orderId": order_data["order_no"], "parcelQty": max(1, q.parcel_count - 1),
             })},
            {"key": "waybill_fee", "title": "清单运费查询（揽收后）",
             **client.build_gateway_preview(client.SVC_QUERY_WAYBILL, {
                 "trackingType": "2", "trackingNum": "SF<正式下单返回的运单号>",
             })},
            {"key": "routes", "title": "路由查询（运单进度）",
             **client.build_gateway_preview(client.SVC_SEARCH_ROUTES, {
                 "trackingType": "1", "trackingNumber": ["SF<正式下单返回的运单号>"], "methodType": "1",
             })},
            {"key": "cancel", "title": "取消订单（用户撤销时）",
             **client.build_gateway_preview(client.SVC_UPDATE_ORDER, {
                 "dealType": 2, "orderId": order_data["order_no"], "language": "zh-CN",
             })},
        ],
    }


@router.post("/db/request-preview")
def db_request_preview(q: SFRequestPreviewQuery, db: Session = Depends(get_db)):
    """Generate signed Deppon price and waybill-fee previews without sending them."""
    from courier import get_courier

    from utils.regions import normalize_region
    client = get_courier("DB", db, normalize_region(q.debug_region_code))
    order_data = q.model_dump()
    order_data["weight"] = q.item_weight
    order_data["volume"] = q.item_volume
    try:
        quote_payload = client._build_price_query_params(order_data)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc))
    product_titles = {
        "TZKJC": "小件特快", "XJBK": "小件标快",
        "HKDJC": "大件特快（次日）", "HKDJG": "大件特快（隔日备选）",
        "DJBK": "大件标快",
    }

    quote_requests = []
    for candidates in client._quote_product_groups(q.item_weight):
        for candidate_index, product_code in enumerate(candidates):
            product_payload = dict(quote_payload)
            product_payload["productCode"] = product_code
            key = "quote" if not quote_requests else f"quote_{product_code.lower()}"
            title = f"估算价格：{product_titles.get(product_code, product_code)}"
            if candidate_index:
                title += "（主产品不可达时发送）"
            quote_requests.append({
                "key": key, "title": title,
                **client.build_gateway_preview(client.price_query_url, product_payload),
            })
    return {
        "ok": True,
        "requests": quote_requests + [
            {"key": "waybill_fee", "title": "运费查询（揽收后刷新订单时发送）",
             **client.build_gateway_preview(client.waybill_fee_url, {
                 "mailNo": "DPK<正式下单返回的运单号>",
             })},
        ],
    }


@router.post("/jdl/request-preview")
def jdl_request_preview(q: SFRequestPreviewQuery, db: Session = Depends(get_db)):
    """Show the JD Logistics request chain without calling the production API."""
    from courier import get_courier
    from utils.regions import normalize_region

    client = get_courier("JDL", db, normalize_region(q.debug_region_code))
    order_data = q.model_dump()
    order_data.update({
        "order_no": "CO<提交时由后端生成32位UUID>",
        "weight": q.item_weight,
        "volume": q.item_volume,
    })
    precheck = client._build_precheck_payload(order_data, q.express_type)
    create = client._build_create_order_payload(order_data, q.express_type)
    waybill = "JD<正式下单返回的运单号>"

    def request_item(key, title, path, payload):
        return {
            "key": key, "title": title, "url": f"{client.base_url}{path}",
            "method": "POST", "contentType": "application/json;charset=UTF-8",
            "form": payload, "note": "签名、AppKey 和 Access Token 由服务器发送时加入，不在页面展示。",
        }

    common = {"waybillCode": waybill, "orderCode": order_data["order_no"],
              "orderOrigin": 1, "customerCode": client.customer_code}
    return {"ok": True, "requests": [
        request_item("pre_order", "下单前置校验（自动发送）", "/ecap/v1/orders/precheck", precheck),
        request_item("create_order", "正式下单（审批通过后才发送）", "/ecap/v1/orders/create", create),
        request_item("search_order", "订单状态查询", "/ecap/v1/orders/status/get", common),
        request_item("routes", "标准物流轨迹查询（定时及手动刷新）", "/jd/tracking/query", {
            "referenceNumber": waybill, "referenceType": "20000",
            "customerCode": client.customer_code, "scope": 2,
        }),
        request_item("cancel", "取消订单（用户撤销时）", "/ecap/v1/orders/cancel", {
            **common, "cancelType": 1, "cancelReason": "用户取消", "cancelReasonCode": "1",
        }),
    ]}


@router.post("/fee")
def estimate_fee(q: FeeQuery, db: Session = Depends(get_db)):
    if q.weight is None or q.weight <= 0:
        raise HTTPException(422, "请先填写大于0kg的预估重量")
    try:
        from courier import get_courier
        courier = get_courier(q.courier_type, db)
        return courier.query_fee(
            sender={"province": q.sender_province, "city": q.sender_city},
            receiver={"province": q.receiver_province, "city": q.receiver_city},
            weight=q.weight)
    except Exception as e:
        raise HTTPException(400, str(e))


@router.get("/track/{courier_order_no}")
def track(courier_order_no: str, courier_type: str = "SF", db: Session = Depends(get_db)):
    try:
        from courier import get_courier
        return {"ok": True, "tracks": get_courier(courier_type, db).query_track(courier_order_no)}
    except Exception as e:
        raise HTTPException(400, str(e))


# ================================================================
# 顺丰 路由推送回调 (RoutePushService)
# 顺丰有新轨迹时 POST 到这里 (JSON 或 form)
# 注册后, 沙箱/生产环境都会推
# ================================================================
@router.post("/sf/route-push")
async def sf_route_push(request: Request, db: Session = Depends(get_db)):
    """
    顺丰路由推送回调端点
    body 格式(JSON 模式):
    {
      "Body": {
        "WaybillRoute": [
          {"mailno":"SFxxx", "orderid":"COxxx", "acceptTime":"2026-09-01 12:00:00",
           "acceptAddress":"厦门", "remark":"顺丰速运 已收取快件 快递员:王师傅",
           "opCode":"50", "id":"123"}
        ]
      }
    }
    """
    from config.settings import settings

    config_row = db.query(models.SystemConfig).filter(
        models.SystemConfig.config_key == "sf_check_word"
    ).first()
    check_word = str(
        (config_row.config_value if config_row else None) or settings.sf_check_word or ""
    ).strip()
    if not check_word:
        raise HTTPException(503, "顺丰生产校验码未配置")
    try:
        raw_body = await request.body()
        supplied_verify_code = (
            request.headers.get("X-SF-Verify-Code")
            or request.query_params.get("verifyCode")
            or ""
        )
        signed_content = raw_body.decode("utf-8", errors="strict")
        try:
            body = json.loads(signed_content)
        except Exception:
            form = await request.form()
            supplied_verify_code = supplied_verify_code or str(
                form.get("verifyCode") or form.get("verify_code") or ""
            )
            body_str = str(form.get("content") or form.get("xml") or "")
            signed_content = body_str
            try:
                body = json.loads(body_str) if body_str else {}
            except Exception:
                body = _parse_sf_route_xml(body_str)

        expected_verify_code = base64.b64encode(
            hashlib.md5((signed_content + check_word).encode("utf-8")).digest()
        ).decode("ascii")
        if not supplied_verify_code or not hmac.compare_digest(
            supplied_verify_code, expected_verify_code
        ):
            raise HTTPException(401, "顺丰路由推送校验失败")

        routes = (body.get("Body") or {}).get("WaybillRoute") or body.get("WaybillRoute") or []
        if isinstance(routes, dict):
            routes = [routes]
        count = 0
        for r in routes:
            mailno = r.get("mailno") or r.get("mailNo")
            orderid = r.get("orderid") or r.get("orderId")
            accept_time_str = r.get("acceptTime", "")
            accept_addr = r.get("acceptAddress", "")
            remark = r.get("remark", "")
            op_code = r.get("opCode", "")
            route_id = r.get("id", "")

            # 必须同时匹配本地订单号和运单号，避免只猜中一个标识就能篡改订单。
            if not orderid or not mailno:
                log.warning("忽略缺少 orderid/mailno 的顺丰路由推送")
                continue
            order = db.query(models.Order).filter(
                models.Order.order_no == orderid,
                models.Order.courier_order_no == mailno,
            ).first()
            if not order:
                continue

            # 去重: 同一个 opCode 不重复写 (route_id 可用于更精确去重, 存到 track_detail 里)
            if route_id:
                dup = db.query(models.CourierTrack).filter(
                    models.CourierTrack.order_id == order.id,
                    models.CourierTrack.track_detail.contains(str(route_id))
                ).first()
                if dup:
                    continue

            # 解析时间
            try:
                t = dt.datetime.strptime(accept_time_str, "%Y-%m-%d %H:%M:%S") if accept_time_str else dt.datetime.now()
            except Exception:
                t = dt.datetime.now()

            status_by_code = {
                "50": ("已揽收", "picked"),
                "30": ("派送中", "delivering"),
                "80": ("已签收", "delivered"),
                "31": ("在途", "transit"),
                "99": ("在途", "transit"),
            }
            status_label, category_hint = status_by_code.get(
                str(op_code), (remark or "物流更新", "")
            )
            track_item = {
                "status": status_label,
                "detail": f"{remark}{(' @ ' + accept_addr) if accept_addr else ''}",
                "category": category_hint,
            }
            # 写轨迹
            db.add(models.CourierTrack(
                order_id=order.id, courier_order_no=mailno or order.courier_order_no,
                track_time=t, track_status=status_label,
                track_detail=f"[{route_id}] {remark} @ {accept_addr}"
            ))

            # 更新订单状态 + 快递员信息
            order.track_update_time = dt.datetime.now()

            courier_info = _extract_courier_from_remark(remark)
            from utils.order_status import apply_latest_track_state
            apply_latest_track_state(order, track_item)
            if op_code == "50":
                if courier_info.get("name"):
                    order.courier_picked_by = courier_info["name"]
                if courier_info.get("phone"):
                    order.courier_picked_by_phone = courier_info["phone"]
            elif op_code == "30":
                if courier_info.get("name") and not order.courier_picked_by:
                    order.courier_picked_by = courier_info["name"]

            count += 1

        db.commit()
        return JSONResponse({"return_code": "0000", "return_msg": f"ok, {count} records"})

    except HTTPException:
        raise
    except Exception:
        db.rollback()
        log.exception("处理顺丰路由推送失败")
        return JSONResponse(status_code=500, content={
            "return_code": "1000", "return_msg": "internal error",
        })


def _parse_sf_route_xml(content: str) -> dict:
    """兼容丰桥 RoutePushService 的 form/content XML 格式。"""
    if not str(content or "").strip():
        return {}
    root = ET.fromstring(content)
    routes = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].lower() != "waybillroute":
            continue
        item = dict(element.attrib)
        for child in element:
            item[child.tag.rsplit("}", 1)[-1]] = child.text or ""
        routes.append(item)
    return {"WaybillRoute": routes}


def _extract_courier_from_remark(remark: str) -> dict:
    """从路由 remark 里挖快递员姓名和电话"""
    if not remark:
        return {"name": "", "phone": ""}
    name, phone = "", ""
    m = re.search(r"(?:快递员|揽收员|派件员|收派员|小哥)[：: ]?([\u4e00-\u9fa5A-Za-z]{2,8})", remark)
    if m:
        name = m.group(1)
    p = re.search(r"1[3-9]\d{9}", remark)
    if p:
        phone = p.group(0)
    return {"name": name, "phone": phone}


# ========== 地址簿 (沿用) ==========

@router.get("/addresses")
def list_addresses(
    request: Request, scope: str = "personal", role: str = "receiver",
    db: Session = Depends(get_db),
):
    if scope != "personal":
        raise HTTPException(403, "企业地址请通过企业寄件地址接口读取")
    if role != "receiver":
        raise HTTPException(400, "个人地址簿仅提供收件地址")
    current_user = getattr(request.state, "current_user", None)
    user_id = int(current_user["id"]) if current_user else None
    addrs = db.query(models.Address).filter(
        models.Address.scope == "personal", models.Address.role == "receiver",
        models.Address.user_id == user_id,
    ).order_by(models.Address.is_default.desc(), models.Address.id.desc()).all()
    return {"ok": True, "items": [{"id": a.id, "label": a.label, "name": a.name,
                "phone": a.phone, "company_name": a.company_name, "province": a.province,
                "city": a.city, "district": a.district, "detail": a.detail} for a in addrs]}


class AddressCreate(BaseModel):
    scope: str = "personal"; role: str = "receiver"; label: str = ""
    name: str; phone: str; company_name: Optional[str] = None
    province: Optional[str] = None; city: Optional[str] = None; district: Optional[str] = None; detail: str

    @field_validator("phone", mode="before")
    @classmethod
    def validate_contact_phone(cls, value):
        from utils.contact import normalize_contact_number
        return normalize_contact_number(value)


@router.post("/addresses")
def create_address(addr: AddressCreate, request: Request, db: Session = Depends(get_db)):
    if addr.scope != "personal" or addr.role != "receiver":
        raise HTTPException(400, "个人地址簿仅支持收件地址")
    current_user = getattr(request.state, "current_user", None)
    user_id = int(current_user["id"]) if current_user else None
    existing = db.query(models.Address).filter(
        models.Address.user_id == user_id,
        models.Address.scope == "personal", models.Address.role == "receiver",
        models.Address.phone == addr.phone, models.Address.province == addr.province,
        models.Address.city == addr.city, models.Address.district == addr.district,
        models.Address.detail == addr.detail,
    ).first()
    if existing:
        return {"ok": True, "id": existing.id, "existing": True}
    a = models.Address(user_id=user_id, scope="personal", role="receiver", label=addr.label,
        name=addr.name, phone=addr.phone, company_name=addr.company_name,
        province=addr.province, city=addr.city, district=addr.district, detail=addr.detail)
    db.add(a); db.commit(); db.refresh(a)
    return {"ok": True, "id": a.id}


@router.delete("/addresses/{addr_id}")
def delete_address(addr_id: int, request: Request, db: Session = Depends(get_db)):
    current_user = getattr(request.state, "current_user", None)
    user_id = int(current_user["id"]) if current_user else None
    a = db.query(models.Address).filter(
        models.Address.id == addr_id, models.Address.scope == "personal",
        models.Address.user_id == user_id,
    ).first()
    if not a: raise HTTPException(404, "Not found")
    db.delete(a); db.commit(); return {"ok": True}
