"""Regional routing for employees, carrier settings, addresses and accounting."""
from __future__ import annotations

import json

from database import models


XIAMEN_OTHER = "xiamen_other"
CHENGDU = "chengdu"
BEIJING = "beijing"
DEFAULT_REGION = XIAMEN_OTHER
REGIONS = (
    (XIAMEN_OTHER, "厦门及其他"),
    (CHENGDU, "成都"),
    (BEIJING, "北京"),
)
REGION_CODES = {code for code, _ in REGIONS}
REGION_LABELS = dict(REGIONS)
DEFAULT_CARRIERS = {
    XIAMEN_OTHER: ["SF", "DB"],
    CHENGDU: ["JDL"],
    BEIJING: [],
}
DEFAULT_RECOMMENDED_SERVICES = {
    XIAMEN_OTHER: "db_small_standard",
    CHENGDU: "jdl_biaokuai",
    BEIJING: "db_small_standard",
}
DEFAULT_SERVICE_CATALOG_MIGRATION_KEY = "migration.default_recommended_services.v1"


def normalize_region(value: str | None) -> str:
    value = str(value or "").strip().lower()
    aliases = {
        "xiamen": XIAMEN_OTHER, "xiamen_other": XIAMEN_OTHER, "厦门": XIAMEN_OTHER,
        "厦门及其他": XIAMEN_OTHER, "other": XIAMEN_OTHER, "其他": XIAMEN_OTHER,
        "chengdu": CHENGDU, "成都": CHENGDU,
        "beijing": BEIJING, "北京": BEIJING,
    }
    return aliases.get(value, value if value in REGION_CODES else DEFAULT_REGION)


def region_from_work_location(value: str | None) -> str:
    text = str(value or "").strip()
    if "成都" in text:
        return CHENGDU
    if "北京" in text:
        return BEIJING
    return XIAMEN_OTHER


def region_key(region_code: str, key: str) -> str:
    return f"region.{normalize_region(region_code)}.{key}"


def get_config(db, region_code: str, key: str, default: str = "") -> str:
    row = db.query(models.SystemConfig).filter(
        models.SystemConfig.config_key == region_key(region_code, key)
    ).first()
    return str(row.config_value if row and row.config_value is not None else default).strip()


def save_config(db, region_code: str, key: str, value, description: str = ""):
    config_key = region_key(region_code, key)
    row = db.query(models.SystemConfig).filter(
        models.SystemConfig.config_key == config_key
    ).first()
    if row:
        row.config_value = str(value)
        if description:
            row.description = description
    else:
        db.add(models.SystemConfig(
            config_key=config_key, config_value=str(value), description=description,
        ))


def enabled_carriers(db, region_code: str) -> list[str]:
    raw = get_config(db, region_code, "enabled_couriers", "")
    if raw:
        try:
            values = json.loads(raw)
            if isinstance(values, list):
                return [str(value).upper() for value in values if str(value).upper() in {"SF", "DB", "JDL"}]
        except (TypeError, ValueError):
            pass
    return list(DEFAULT_CARRIERS[normalize_region(region_code)])


def save_enabled_carriers(db, region_code: str, values) -> None:
    normalized = [code for code in ("SF", "DB", "JDL") if code in {str(v).upper() for v in values}]
    save_config(db, region_code, "enabled_couriers", json.dumps(normalized), "区域启用的快递服务")


def available_services(db, region_code: str) -> list[dict]:
    """Return the product catalog currently exposed for one region."""
    from courier_services import ALL_SERVICES, available_in_region

    region_code = normalize_region(region_code)
    allowed = set(enabled_carriers(db, region_code))
    return [
        service for service in ALL_SERVICES
        if service["courier"] in allowed and available_in_region(service, region_code)
    ]


def default_service_id(db, region_code: str) -> str | None:
    """Return the configured fixed recommendation, constrained to the regional catalog."""
    region_code = normalize_region(region_code)
    service_ids = {service["id"] for service in available_services(db, region_code)}
    configured = get_config(db, region_code, "default_service_id", "")
    if configured in service_ids:
        return configured
    preferred = DEFAULT_RECOMMENDED_SERVICES.get(region_code, "")
    if preferred in service_ids:
        return preferred
    return next(iter(service_ids), None)


def save_default_service_id(db, region_code: str, service_id: str) -> None:
    region_code = normalize_region(region_code)
    service_id = str(service_id or "").strip()
    if service_id not in {service["id"] for service in available_services(db, region_code)}:
        raise ValueError("默认推荐必须是该地区当前可用的快递产品")
    save_config(db, region_code, "default_service_id", service_id, "区域默认推荐快递产品")


def ensure_default_service_catalog(db) -> bool:
    """One-time upgrade ensuring every shipped regional default is actually selectable."""
    marker = db.query(models.SystemConfig).filter(
        models.SystemConfig.config_key == DEFAULT_SERVICE_CATALOG_MIGRATION_KEY
    ).first()
    if marker:
        return False

    from courier_services import SERVICE_MAP

    for region_code, service_id in DEFAULT_RECOMMENDED_SERVICES.items():
        service = SERVICE_MAP.get(service_id)
        if not service:
            continue
        carriers = enabled_carriers(db, region_code)
        if service["courier"] not in carriers:
            save_enabled_carriers(db, region_code, [*carriers, service["courier"]])
    db.add(models.SystemConfig(
        config_key=DEFAULT_SERVICE_CATALOG_MIGRATION_KEY,
        config_value="1",
        description="补齐各地区默认推荐产品对应快递服务",
    ))
    return True


def user_region(db, current_user) -> str:
    if not current_user or not current_user.get("id"):
        return DEFAULT_REGION
    user = db.query(models.User).filter(models.User.id == int(current_user["id"])).first()
    return normalize_region(getattr(user, "region_code", None))


def request_region(db, current_user, requested_region=None, auth_enabled=True) -> str:
    """正式环境固定使用员工工作地区；仅关闭钉钉登录的调试环境允许页面预览切换。"""
    if not auth_enabled:
        requested = str(requested_region or "").strip().lower()
        if requested in REGION_CODES:
            return requested
    return user_region(db, current_user)


def region_options() -> list[dict]:
    return [{"code": code, "label": label} for code, label in REGIONS]
