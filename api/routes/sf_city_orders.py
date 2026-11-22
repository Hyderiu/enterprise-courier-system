"""Employee-facing SF City shop-mode quotes and approval submissions."""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from courier import get_courier
from config.settings import settings
from database import models
from database.connection import get_db
from utils.approval_notifications import (
    queue_approval_notification, queue_approval_progress_notification,
)
from utils.approval_policy import assign_approval
from utils.regions import BEIJING, XIAMEN_OTHER, request_region
from utils.sf_city_access import require_sf_city_user


router = APIRouter()
logger = logging.getLogger(__name__)
# 物品类型是店铺签约配置。2026-11-13 通过正式 getshopinfo 核验：
# 厦门店铺开通 10 快件、36 证照；北京店铺开通 3 百货、12 文件。
# 页面固定按区域展示，避免每次进入时因接口波动而改变选项；正式报价和
# 下单仍由顺丰再次校验店铺权限。
REGION_PRODUCT_TYPES = {
    XIAMEN_OTHER: {10: "快件", 36: "证照"},
    BEIJING: {3: "百货", 12: "文件"},
}
PRODUCT_TYPES = {
    code: label for region_types in REGION_PRODUCT_TYPES.values()
    for code, label in region_types.items()
}


def _friendly_sf_city_error(exc: Exception, action: str = "校验") -> str:
    """Convert upstream API errors into employee-facing Chinese guidance."""
    raw = str(exc or "").strip()
    lowered = raw.lower()
    code_match = re.search(r"(?:请求失败\s*)?([^\s:：]+)\s*[:：]", raw)
    code = code_match.group(1) if code_match else ""
    suffix = f"（顺丰错误码：{code}）" if code and code != "顺丰同城" else ""

    if code == "1004":
        return (
            "顺丰本次未通过订单信息校验，请先重新获取报价；如果仍失败，"
            "请从地图重新复制更精确的坐标，并确认区县、详细地址与定位一致。"
            + suffix
        )
    if code == "3206":
        return (
            "顺丰当前店铺无法为该地址报价，请确认收件地址与定位均在当前区域且相互一致。"
            + suffix
        )

    rules = (
        (("跨城", "城市不一致", "非同城"),
         "收件地址与取件店铺不在同一城市，暂不支持顺丰同城配送。"),
        (("超出配送范围", "不在配送范围", "不支持配送", "无法配送", "不可配送", "配送距离"),
         "该收件地址暂不在顺丰同城配送范围内，请核对地址和定位，或改用普通快递。"),
        (("经纬度", "坐标", "定位"),
         "收件地址定位无效或与填写地址不一致，请重新从地图获取准确坐标。"),
        (("地址解析", "地址无效", "地址错误", "地址不完整", "详细地址"),
         "收件地址无法识别，请补充区县、道路、楼栋及门牌号后重试。"),
        (("超重", "重量超", "重量限制"),
         "物品重量超出当前同城产品可承运范围，请修改重量或改用普通快递。"),
        (("暂无骑士", "无骑士", "暂无运力", "运力不足"),
         "当前区域暂时没有可用骑士，请稍后重新获取报价。"),
        (("产品类型", "物品类型", "品类"),
         "当前顺丰同城店铺未开通所选物品类别，请改选页面提供的其他类别。"),
        (("店铺", "门店", "未绑定", "未授权"),
         "顺丰同城店铺或月结配置暂不可用，请联系管理员检查绑定信息。"),
        (("频繁", "限流", "繁忙", "timeout", "timed out", "超时", "connection"),
         "顺丰同城接口暂时繁忙，请稍后重新获取报价。"),
        (("参数", "必填", "不能为空", "格式"),
         "顺丰同城未通过订单信息校验，请检查地址、定位、重量和物品信息。"),
    )
    for keywords, message in rules:
        if any(keyword.lower() in lowered for keyword in keywords):
            return message + suffix
    return f"顺丰同城暂时无法完成{action}，请稍后重试；如持续失败请联系管理员。{suffix}"


def _contact_phone(value) -> str:
    """系统记录使用的联系电话。

    允许手机、座机及分机的常见写法，但至少需要 6 位数字，
    避免把无法拨打的文字传给骑手。
    """
    normalized = re.sub(r"\s+", "", str(value or "").strip())
    if len(re.sub(r"\D", "", normalized)) < 6 or len(normalized) > 24:
        raise ValueError("请输入可联系的电话号码")
    if not re.fullmatch(r"[0-9+()\-]+", normalized):
        raise ValueError("联系电话格式不正确")
    return normalized


def _coordinate(value, label: str, minimum: float, maximum: float) -> str:
    raw = str(value or "").strip()
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        raise ValueError(f"{label}格式不正确")
    try:
        number = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{label}格式不正确")
    if not minimum <= number <= maximum:
        raise ValueError(f"{label}超出有效范围")
    # 地图工具复制多少位就原样传递多少位，不人为补齐或截断精度。
    return raw


class SFCityQuote(BaseModel):
    receiver_address: str
    receiver_city: str = "厦门市"
    receiver_lng: str
    receiver_lat: str
    item_weight: float = 1
    # 店铺接口要求商品总金额，但企业文件寄送不要求员工填写商品售价。
    # 适配器在非保价场景传 1 分名义金额；此字段仅保留兼容旧请求。
    item_value: float = 0.01
    product_type: int = 10
    insurance_amount: float = 0

    @field_validator("receiver_lng", mode="before")
    @classmethod
    def valid_lng(cls, value):
        return _coordinate(value, "经度", -180, 180)

    @field_validator("receiver_lat", mode="before")
    @classmethod
    def valid_lat(cls, value):
        return _coordinate(value, "纬度", -90, 90)


class SFCityOrderCreate(SFCityQuote):
    sender_name: str
    sender_phone: str
    receiver_name: str
    receiver_phone: str
    receiver_company: Optional[str] = None
    receiver_province: str = "福建省"
    receiver_district: str = "思明区"
    item_name: str
    item_count: int = 1
    item_remark: Optional[str] = None
    shipping_reason: str
    cost_type: str = "department"
    dept_code: Optional[str] = None
    dept_name: Optional[str] = None
    project_code: Optional[str] = None
    project_name: Optional[str] = None

    @field_validator("receiver_phone", mode="before")
    @classmethod
    def valid_phone(cls, value):
        return _contact_phone(value)

    @field_validator("sender_phone", mode="before")
    @classmethod
    def valid_sender_phone(cls, value):
        return _contact_phone(value)


def _client(db: Session, region_code: str):
    try:
        return get_courier("SF_CITY", db, region_code)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, f"顺丰同城配置不完整：{exc}")


def _precheck_with_transient_retry(client, payload: dict) -> dict:
    """Retry one upstream 1004 once; identical data often succeeds immediately."""
    try:
        return client.precheck_order(payload)
    except Exception as exc:
        if not re.search(r"(?:^|\s)1004(?:\s|:|：)", str(exc or "")):
            raise
        logger.info("Retrying SF City precheck once after upstream 1004")
        return client.precheck_order(payload)


REGION_ADDRESS = {
    XIAMEN_OTHER: {"province": "福建省", "city": "厦门市", "district": "思明区"},
    BEIJING: {"province": "北京市", "city": "北京市", "district": ""},
}


def _preview_shop(region_code: str) -> dict:
    address = REGION_ADDRESS.get(region_code, REGION_ADDRESS[XIAMEN_OTHER])
    is_beijing = region_code == BEIJING
    return {
        "shop_id": "DEMO-BJ-CITY" if is_beijing else "DEMO-XM-CITY",
        "shop_name": "北京演示取件点" if is_beijing else "厦门演示取件点",
        "shop_address": "演示路 88 号一层取件处",
        "path_name": f"{address['province']}{address['city']}{address['district']}",
        "longitude": "116.397" if is_beijing else "118.089",
        "latitude": "39.908" if is_beijing else "24.479",
        "settlement_type": "企业月结演示",
        "shop_product_types": REGION_PRODUCT_TYPES.get(region_code, {}),
    }


def _preview_quote(body: SFCityQuote, region_code: str) -> dict:
    _quote_data(body, region_code)
    base_fee = 28.0 if region_code == BEIJING else 18.0
    extra_weight = max(float(body.item_weight or 1) - 1, 0)
    insurance_fee = 1.0 if float(body.insurance_amount or 0) > 0 else 0.0
    return {
        "estimated_fee": round(base_fee + extra_weight * 2 + insurance_fee, 2),
        "promise_delivery_minutes": 90 if region_code == BEIJING else 60,
        "delivery_distance_meter": 6800 if region_code == BEIJING else 4200,
        "preview": True,
    }

# 仅用于拦截明显选错城市的坐标，边界刻意放宽，不替代顺丰的正式服务范围校验。
REGION_COORDINATE_BOUNDS = {
    XIAMEN_OTHER: {"lng": (117.7, 118.8), "lat": (24.2, 25.1), "label": "厦门市"},
    BEIJING: {"lng": (115.6, 117.6), "lat": (39.3, 41.2), "label": "北京市"},
}


def _validate_region_coordinate(body: SFCityQuote, region_code: str) -> None:
    bounds = REGION_COORDINATE_BOUNDS.get(region_code)
    if not bounds:
        return
    lng, lat = float(body.receiver_lng), float(body.receiver_lat)
    if not (bounds["lng"][0] <= lng <= bounds["lng"][1] and
            bounds["lat"][0] <= lat <= bounds["lat"][1]):
        raise HTTPException(
            400,
            f"当前收件定位不在{bounds['label']}，请重新复制该城市收件地点的地图坐标",
        )


def _quote_data(body: SFCityQuote, region_code: str = XIAMEN_OTHER) -> dict:
    if body.item_weight <= 0:
        raise HTTPException(400, "预估重量必须大于0")
    if body.insurance_amount < 0:
        raise HTTPException(400, "保价声明价值不能小于0")
    if not str(body.receiver_address or "").strip():
        raise HTTPException(400, "请填写完整收件地址")
    region_address = REGION_ADDRESS.get(region_code, REGION_ADDRESS[XIAMEN_OTHER])
    city_keyword = region_address["city"].replace("市", "")
    if city_keyword not in str(body.receiver_city or ""):
        raise HTTPException(400, f"顺丰同城急送目前仅支持{region_address['city']}内配送")
    _validate_region_coordinate(body, region_code)
    region_types = REGION_PRODUCT_TYPES.get(region_code, {})
    if body.product_type not in region_types:
        raise HTTPException(400, "请选择当前区域店铺已开通的物品类型")
    return {
        "receiver_province": getattr(body, "receiver_province", None) or region_address["province"],
        "receiver_city": body.receiver_city,
        "receiver_district": getattr(body, "receiver_district", ""),
        "receiver_address": body.receiver_address,
        "receiver_lng": body.receiver_lng,
        "receiver_lat": body.receiver_lat,
        "item_weight": body.item_weight,
        "item_value": 0.01,
        "express_type": str(body.product_type),
        "insurance_amount": body.insurance_amount,
    }


@router.get("/shop")
def shop_info(request: Request, db: Session = Depends(get_db)):
    region = request_region(
        db, request.state.current_user, request.query_params.get("region"),
        auth_enabled=bool(getattr(request.state, "auth_enabled", False)),
    )
    require_sf_city_user(
        db, request.state.current_user or {}, region_code=region,
        auth_enabled=bool(getattr(request.state, "auth_enabled", False)),
    )
    if settings.preview_safe_mode:
        info = _preview_shop(region)
    else:
        try:
            info = _client(db, region).shop_info()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"顺丰同城店铺信息读取失败：{exc}")
    return {"ok": True, "shop": {
        "address": info.get("shop_address") or "",
        "city": info.get("path_name") or "",
        "longitude": info.get("longitude") or "",
        "latitude": info.get("latitude") or "",
        "settlement_type": info.get("settlement_type"),
        "product_types": [
            {"value": code, "label": label}
            for code, label in REGION_PRODUCT_TYPES.get(region, {}).items()
        ],
    }}


@router.post("/quote")
def quote(body: SFCityQuote, request: Request, db: Session = Depends(get_db)):
    query_params = getattr(request, "query_params", {})
    region = request_region(
        db, request.state.current_user, query_params.get("region"),
        auth_enabled=bool(getattr(request.state, "auth_enabled", False)),
    )
    require_sf_city_user(
        db, request.state.current_user or {}, region_code=region,
        auth_enabled=bool(getattr(request.state, "auth_enabled", False)),
    )
    if settings.preview_safe_mode:
        result = _preview_quote(body, region)
    else:
        try:
            result = _precheck_with_transient_retry(
                _client(db, region), _quote_data(body, region)
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("SF City quote failed: %s", exc, exc_info=True)
            raise HTTPException(422, _friendly_sf_city_error(exc, "报价")) from exc
    return {"ok": True, **result}


@router.post("/orders")
def create_order(
    body: SFCityOrderCreate, request: Request, background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    current_user = request.state.current_user or {}
    query_params = getattr(request, "query_params", {})
    region = request_region(
        db, current_user, query_params.get("region"),
        auth_enabled=bool(getattr(request.state, "auth_enabled", False)),
    )
    require_sf_city_user(
        db, current_user, region_code=region,
        auth_enabled=bool(getattr(request.state, "auth_enabled", False)),
    )
    if body.item_count < 1:
        raise HTTPException(400, "物品数量必须大于0")
    if not str(body.sender_name or "").strip():
        raise HTTPException(400, "请填写寄件人姓名")
    if not str(body.receiver_name or "").strip():
        raise HTTPException(400, "请填写收件人姓名")
    if not str(body.item_name or "").strip():
        raise HTTPException(400, "请填写物品名称")
    if not str(body.shipping_reason or "").strip():
        raise HTTPException(400, "请填写寄件原因")

    cost_type = str(body.cost_type or "").strip().lower()
    if cost_type not in {"department", "project"}:
        raise HTTPException(400, "费用归属只能选择部门或项目")
    if cost_type == "project" and not str(body.project_name or "").strip():
        raise HTTPException(400, "请选择费用归属项目")
    if not str(body.dept_name or "").strip():
        body.dept_name = current_user.get("department") or None
    if not str(body.dept_name or "").strip():
        raise HTTPException(400, "请选择费用归属部门")

    quote_payload = _quote_data(body, region)
    if settings.preview_safe_mode:
        shop = _preview_shop(region)
        quote_result = _preview_quote(body, region)
        shop_id = shop["shop_id"]
    else:
        client = _client(db, region)
        try:
            shop = client.shop_info()
            shop_types = shop.get("shop_product_types") or {}
            supported_types = {
                int(code) for code in shop_types
                if str(code).isdigit()
            } if isinstance(shop_types, dict) else set()
            if supported_types and body.product_type not in supported_types:
                raise ValueError("当前区域同城店铺未开通所选物品类别")
            quote_result = _precheck_with_transient_retry(client, quote_payload)
            shop_id = client.shop_id
        except Exception as exc:
            logger.warning("SF City submission precheck failed: %s", exc, exc_info=True)
            raise HTTPException(422, _friendly_sf_city_error(exc, "下单前校验")) from exc

    order_no = "CO" + uuid.uuid4().hex.upper()
    region_address = REGION_ADDRESS.get(region, REGION_ADDRESS[XIAMEN_OTHER])
    shop_path = str(shop.get("path_name") or "")
    shop_district = region_address["district"]
    if region == BEIJING:
        district_match = re.search(r"北京市?([^市区县]+[区县])", shop_path)
        if district_match:
            shop_district = district_match.group(1)
    order = models.Order(
        order_no=order_no, courier_type="SF_CITY", region_code=region,
        billing_account_code=shop_id,
        express_type=str(body.product_type),
        express_type_name=REGION_PRODUCT_TYPES.get(region, {}).get(
            body.product_type, PRODUCT_TYPES.get(body.product_type, f"品类{body.product_type}")
        ),
        # 店铺模式的取件地址由顺丰店铺资料决定；sender 字段保存员工
        # 本次填写的实际寄件联系人。审批申请人仍由 created_by 的钉钉实名确定。
        sender_name=body.sender_name.strip(),
        sender_phone=body.sender_phone,
        sender_company=shop.get("shop_name") or "",
        sender_province=region_address["province"], sender_city=region_address["city"],
        sender_district=shop_district,
        sender_address=shop.get("shop_address") or "顺丰同城登记店铺地址",
        receiver_name=body.receiver_name.strip(), receiver_phone=body.receiver_phone,
        receiver_company=body.receiver_company,
        receiver_province=body.receiver_province, receiver_city=body.receiver_city,
        receiver_district=body.receiver_district,
        receiver_address=body.receiver_address.strip(),
        receiver_lng=body.receiver_lng, receiver_lat=body.receiver_lat,
        item_name=body.item_name.strip(), item_weight=body.item_weight,
        item_value=body.item_value, item_count=body.item_count, parcel_count=1,
        item_remark=body.item_remark, shipping_reason=body.shipping_reason.strip(),
        insurance=body.insurance_amount,
        freight=quote_result.get("estimated_fee"),
        pickup_time_mode="immediate",
        cost_type=cost_type, dept_code=body.dept_code, dept_name=body.dept_name,
        project_code=body.project_code if cost_type == "project" else None,
        project_name=body.project_name if cost_type == "project" else None,
        status="pending", source="sf_city", created_by=current_user.get("id"),
        # Persist the non-ordering precreateorder response for audit.  The
        # formal createorder response replaces it only after final approval.
        order_response=json.dumps(
            {"stage": "precheck", "result": quote_result},
            ensure_ascii=False, default=str,
        ),
    )
    assign_approval(order, db)
    db.add(order)
    db.commit()
    db.refresh(order)
    notification = queue_approval_notification(background_tasks, db, order, "order")
    applicant_notification = queue_approval_progress_notification(
        background_tasks, db, order, "submitted", "order"
    )
    return {
        "ok": True, "id": order.id, "order_no": order.order_no,
        "estimated_fee": order.freight,
        "promise_delivery_minutes": quote_result.get("promise_delivery_minutes"),
        "notification": notification,
        "applicant_notification": applicant_notification,
    }
