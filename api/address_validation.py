"""Validate province/city/district combinations against the bundled local dataset."""
import json
from functools import lru_cache
from pathlib import Path


DIRECT_MUNICIPALITIES = {"北京市", "天津市", "上海市", "重庆市"}
DATA_PATH = Path(__file__).resolve().parent.parent / "web" / "static" / "data" / "pcas-code.json"


@lru_cache(maxsize=1)
def _divisions() -> list[dict]:
    with DATA_PATH.open("r", encoding="utf-8") as source:
        return json.load(source)


def _exact(items: list[dict], name: str) -> dict | None:
    value = (name or "").strip()
    return next((item for item in items if item.get("name") == value), None)


def validate_division(province_name: str, city_name: str, district_name: str) -> str:
    """Return an empty string for a valid combination, otherwise a user-facing error."""
    province = _exact(_divisions(), province_name)
    if not province:
        return "省份不在标准行政区划中"

    if province["name"] in DIRECT_MUNICIPALITIES:
        if city_name.strip() != province["name"]:
            return "直辖市的城市名称应与省级名称一致"
        districts = [district for group in province.get("children", []) for district in group.get("children", [])]
    else:
        city = _exact(province.get("children", []), city_name)
        if not city:
            return "城市不属于所选省份"
        districts = city.get("children", [])

    if not _exact(districts, district_name):
        return "区县不属于所选城市"
    return ""
