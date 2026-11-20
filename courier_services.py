from __future__ import annotations

SF_SERVICES = [
    {
        "id": "sf_tekui", "courier": "SF", "name": "顺丰特快",
        "express_type": "1", "express_name": "T4",
        "tagline": "时效优先，通常比标快更快",
        "difference": "适合文件、电子产品及其他时效敏感或价值较高的中小件；同线路通常价格更高。",
        "weight_min": 0, "weight_max": None, "transit_time": "按寄收线路实时试算",
        "speed_level": "fast", "size_class": "small", "default_on": True,
    },
    {
        "id": "sf_biaokuai", "courier": "SF", "name": "顺丰标快",
        "express_type": "2", "express_name": "T6",
        "tagline": "价格更优，时效稳定，覆盖更广",
        "difference": "适合普通文件和日常中小件，在时效要求不紧时兼顾服务与费用。",
        "weight_min": 0, "weight_max": None, "transit_time": "按寄收线路实时试算",
        "speed_level": "standard", "size_class": "small", "default_on": True,
    },
    {
        "id": "sf_same_day", "courier": "SF", "name": "顺丰即日",
        "express_type": "6", "express_name": "顺丰即日",
        "tagline": "当日送达，适合北京同城及可达线路的紧急寄件",
        "difference": "适合对当天送达有明确要求的文件和中小件；是否可用、价格和承诺时效以顺丰实时返回为准。",
        "weight_min": 0, "weight_max": None, "transit_time": "按寄收线路实时试算",
        "speed_level": "same_day", "size_class": "small", "default_on": True,
        "regions": {"beijing"},
        # 已从北京可选产品中下线；保留定义仅用于历史订单名称和编码兼容。
        "catalog_enabled": False,
    },
    {
        "id": "sf_large", "courier": "SF", "name": "顺丰大件",
        "express_type": "255", "express_name": "顺丰卡航",
        "tagline": "大件陆运服务，适合较重或体积较大的货物",
        "difference": "适合约20kg及以上，或单件体积较大的设备、物料和货物；是否可寄及最终费用以当前地址和重量的实时查询结果为准。",
        "weight_min": 20, "weight_max": None, "transit_time": "按寄收线路实时试算",
        "speed_level": "standard", "size_class": "large", "default_on": True,
        "regions": {"beijing"},
    },
]

DB_SERVICES = [
    {
        "id": "db_dajian_fast", "courier": "DB", "name": "大件特快",
        "express_type": "DJTK", "express_name": "大件特快",
        "tagline": "15kg及以上大件，时效优先",
        "difference": "适合较重且较急的货物；相较大件标快优先考虑运输时效。",
        "weight_min": 15, "weight_max": None, "transit_time": "特快时效，以德邦实时试算为准",
        "speed_level": "fast", "size_class": "large", "default_on": True,
    },
    {
        "id": "db_small_fast", "courier": "DB", "name": "小件特快",
        "express_type": "XJTK", "express_name": "小件特快",
        "tagline": "15kg以下小件，时效优先",
        "difference": "适合较轻且较急的货物；相较小件标快优先考虑运输时效。",
        "weight_min": 0, "weight_max": 15, "transit_time": "特快时效，以德邦实时试算为准",
        "speed_level": "fast", "size_class": "small", "default_on": True,
    },
    {
        "id": "db_dajian_standard", "courier": "DB", "name": "大件标快",
        "express_type": "DJBK", "express_name": "大件标快",
        "tagline": "15kg及以上大件，时效与费用更均衡",
        "difference": "适合普通大件和时效要求适中的货物；优先兼顾稳定时效与费用。",
        "weight_min": 15, "weight_max": None, "transit_time": "标准时效，以德邦实时试算为准",
        "speed_level": "standard", "size_class": "large", "default_on": True,
    },
    {
        "id": "db_small_standard", "courier": "DB", "name": "小件标快",
        "express_type": "XJBK", "express_name": "小件标快",
        "tagline": "15kg以下小件，时效与费用更均衡",
        "difference": "适合普通小件和时效要求适中的货物；优先兼顾稳定时效与费用。",
        "weight_min": 0, "weight_max": 15, "transit_time": "标准时效，以德邦实时试算为准",
        "speed_level": "standard", "size_class": "small", "default_on": True,
    },
]

JDL_SERVICES = [
    {
        "id": "jdl_biaokuai", "courier": "JDL", "name": "京东标快",
        "express_type": "ed-m-0001", "express_name": "京东标快",
        "tagline": "覆盖广、时效稳定，兼顾日常寄件费用",
        "difference": "适合日常文件、办公用品及普通中小件，对送达时效要求适中时优先选择。",
        "weight_min": 0, "weight_max": None, "transit_time": "以前置校验返回为准",
        "speed_level": "standard", "size_class": "small", "default_on": True,
    },
    {
        "id": "jdl_tekuai", "courier": "JDL", "name": "京东特快",
        "express_type": "ed-m-0002", "express_name": "京东特快",
        "tagline": "时效优先，适合需要更快送达的寄件",
        "difference": "适合紧急文件、电子产品及其他对送达时效要求较高的中小件。",
        "weight_min": 0, "weight_max": None, "transit_time": "以前置校验返回为准",
        "speed_level": "fast", "size_class": "small", "default_on": True,
    },
]

ALL_SERVICES = SF_SERVICES + DB_SERVICES + JDL_SERVICES
SERVICE_MAP = {s["id"]: s for s in ALL_SERVICES}
COURIER_BRANDS = {"SF": "顺丰", "DB": "德邦", "JDL": "京东"}


def service_display_name(service: dict | None) -> str:
    if not service:
        return ""
    name = str(service.get("name") or "")
    brand = COURIER_BRANDS.get(str(service.get("courier") or ""), "")
    return name if not brand or name.startswith(brand) else f"{brand}{name}"


def available_in_region(service: dict, region_code: str) -> bool:
    """Return whether a product is exposed in the selected regional catalog."""
    if service.get("catalog_enabled") is False:
        return False
    regions = service.get("regions")
    return not regions or str(region_code or "") in regions


def get_by_courier(courier):
    return [s for s in ALL_SERVICES if s["courier"] == str(courier).upper()]


def get_default_enabled():
    return [s["id"] for s in ALL_SERVICES]


def filter_by_weight(courier, weight, enabled_ids=None):
    result = []
    for s in get_by_courier(courier):
        if enabled_ids and s["id"] not in enabled_ids:
            continue
        if s["weight_min"] <= weight <= (s["weight_max"] or 1e9):
            result.append(s)
    return result


def estimate_chargeable_weight(weight: float, volume: float | None = None) -> tuple[float, float]:
    """Estimate chargeable weight with Deppon's public 6000 volumetric divisor."""
    actual_weight = max(float(weight or 0), 0)
    volume_m3 = max(float(volume or 0), 0)
    volumetric_weight = volume_m3 * 1_000_000 / 6000
    return max(actual_weight, volumetric_weight), volumetric_weight
