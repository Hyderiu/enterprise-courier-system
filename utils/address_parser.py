# -*- coding: utf-8 -*-
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

PROVINCES = [
    "北京市","天津市","上海市","重庆市",
    "河北省","山西省","辽宁省","吉林省","黑龙江省",
    "江苏省","浙江省","安徽省","福建省","江西省","山东省",
    "河南省","湖北省","湖南省","广东省","海南省",
    "四川省","贵州省","云南省","陕西省","甘肃省","青海省","台湾省",
    "内蒙古自治区","广西壮族自治区","西藏自治区","宁夏回族自治区","新疆维吾尔自治区",
    "香港特别行政区","澳门特别行政区",
]
PROVINCE_ALIASES = {
    "北京":"北京市","天津":"天津市","上海":"上海市","重庆":"重庆市",
    "河北":"河北省","山西":"山西省","辽宁":"辽宁省","吉林":"吉林省","黑龙江":"黑龙江省",
    "江苏":"江苏省","浙江":"浙江省","安徽":"安徽省","福建":"福建省","江西":"江西省","山东":"山东省",
    "河南":"河南省","湖北":"湖北省","湖南":"湖南省","广东":"广东省","海南":"海南省",
    "四川":"四川省","贵州":"贵州省","云南":"云南省","陕西":"陕西省","甘肃":"甘肃省","青海":"青海省",
    "台湾":"台湾省","内蒙古":"内蒙古自治区","广西":"广西壮族自治区",
    "西藏":"西藏自治区","宁夏":"宁夏回族自治区","新疆":"新疆维吾尔自治区",
    "香港":"香港特别行政区","澳门":"澳门特别行政区",
}
MAJOR_CITIES = {
    "北京":"北京市","上海":"上海市","天津":"天津市","重庆":"重庆市",
    "广州":"广州市","深圳":"深圳市","杭州":"杭州市","成都":"成都市",
    "武汉":"武汉市","南京":"南京市","西安":"西安市","苏州":"苏州市",
    "长沙":"长沙市","沈阳":"沈阳市","青岛":"青岛市","郑州":"郑州市",
    "大连":"大连市","东莞":"东莞市","宁波":"宁波市","厦门":"厦门市",
    "福州":"福州市","合肥":"合肥市","济南":"济南市",
    "昆明":"昆明市","哈尔滨":"哈尔滨市","长春":"长春市","石家庄":"石家庄市",
    "太原":"太原市","贵阳":"贵阳市","南宁":"南宁市","兰州":"兰州市",
    "银川":"银川市","呼和浩特":"呼和浩特市","乌鲁木齐":"乌鲁木齐市",
    "拉萨":"拉萨市","海口":"海口市","三亚":"三亚市",
}
COMMON_DISTRICTS = [
    "朝阳区","海淀区","东城区","西城区","丰台区","通州区","昌平区",
    "浦东新区","徐汇区","黄浦区","静安区","长宁区","虹口区","杨浦区","闵行区",
    "天河区","越秀区","海珠区","荔湾区","白云区",
    "南山区","福田区","罗湖区","宝安区","龙岗区",
    "西湖区","上城区","拱墅区","滨江区","余杭区",
    "思明区","湖里区","集美区",
    "玄武区","秦淮区","鼓楼区","建邺区",
    "江汉区","武昌区","洪山区","汉阳区",
    "雨花区","芙蓉区","天心区","岳麓区",
]
PHONE_RE = re.compile(
    r"(?<!\d)(1[3-9]\d{9}|0\d{2,3}[- ]?\d{7,8}(?:\s*(?:转|分机|ext\.?)[\s:：-]*\d{1,6})?)(?!\d)",
    re.IGNORECASE,
)
PHONE_LABELED_RE = re.compile(
    r"(?:电话|手机|联系电话|tel)[\s:：]*(1[3-9]\d{9}|0\d{2,3}[- ]?\d{7,8}(?:\s*(?:转|分机|ext\.?)[\s:：-]*\d{1,6})?)",
    re.IGNORECASE,
)
NAME_LABELED_RE = re.compile(r"(?:姓名|收件人|寄件人|联系人)[\s:：]*([\u4e00-\u9fa5A-Za-z·]{2,8})")
NAME_HONOR_RE = re.compile(r"([\u4e00-\u9fa5]{1,2})(先生|女士|老师|哥|姐|总|主管|经理)")
ADDR_LABEL_RE = re.compile(r"(?:收件地址|地址|收货地址|收货处|地址是)[\s:：]*")
DIVISIONS_PATH = Path(__file__).resolve().parents[1] / "web" / "static" / "data" / "pcas-code.json"
DIRECT_MUNICIPALITIES = {"北京市", "天津市", "上海市", "重庆市"}
HISTORICAL_DIVISION_ALIASES = {
    # Carrier/customer address books commonly retain names from before an
    # administrative change.  Keep these aliases explicit so fuzzy matching
    # does not silently turn an unrelated place name into a district.
    "长泰区": ("长泰县",),
}


@lru_cache(maxsize=1)
def _load_divisions():
    try:
        with DIVISIONS_PATH.open("r", encoding="utf-8") as source:
            data = json.load(source)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _cities_of(province_node):
    if not province_node:
        return []
    if province_node.get("name") in DIRECT_MUNICIPALITIES:
        districts = []
        for group in province_node.get("children") or []:
            districts.extend(group.get("children") or [])
        return [{"name": province_node["name"], "children": districts}]
    return province_node.get("children") or []


def _name_variants(name, level):
    variants = [name]
    suffixes = {
        "province": ("特别行政区", "维吾尔自治区", "壮族自治区", "回族自治区", "自治区", "省", "市"),
        "city": ("自治州", "地区", "市", "盟"),
        "district": ("自治县", "自治旗", "新区", "区", "县", "市", "旗"),
    }[level]
    for suffix in suffixes:
        if name.endswith(suffix):
            short = name[:-len(suffix)]
            if len(short) >= 2:
                variants.append(short)
            break
    return variants


def _find_node(text, nodes, level):
    matches = []
    for node in nodes or []:
        name = node.get("name") or ""
        for variant in _name_variants(name, level):
            position = text.find(variant)
            if position >= 0:
                matches.append((position, -len(variant), 0 if variant == name else 1, node))
    return min(matches, default=(None, None, None, None))[-1]

def _match_phone(text):
    m = PHONE_LABELED_RE.search(text)
    if m: return m.group(1)
    m = PHONE_RE.search(text)
    if m: return m.group(1)
    return None

def _is_plausible_name(value):
    value = (value or "").strip()
    if not value or re.search(r"\d", value):
        return False
    if re.search(r"(?:省|市|区|县|旗|州|盟|街道|镇|乡|村|路|街|巷|号|楼|室|园|景)", value):
        return False
    return bool(
        re.fullmatch(r"[\u4e00-\u9fa5]{2,6}", value)
        or re.fullmatch(r"[A-Za-z\u4e00-\u9fa5·.]{2,20}", value)
    )


def _match_name(text, phone, province="", city="", district=""):
    m = NAME_LABELED_RE.search(text)
    if m: return m.group(1)
    m = NAME_HONOR_RE.search(text)
    if m: return m.group(1) + m.group(2)

    location_variants = []
    if province:
        location_variants.append(province)
        location_variants.extend(
            alias for alias, full_name in PROVINCE_ALIASES.items() if full_name == province
        )
    if city:
        location_variants.extend(_name_variants(city, "city"))
    if district:
        location_variants.extend(_name_variants(district, "district"))

    positions = [text.find(item) for item in location_variants if item and item in text]
    if positions:
        prefix = text[:min(positions)]
        if phone:
            prefix = prefix.replace(phone, " ")
        prefix = re.sub(r"(?:姓名|收件人|寄件人|联系人|手机|电话)[\s:：]*", " ", prefix)
        candidates = re.findall(r"[A-Za-z\u4e00-\u9fa5·.]{2,20}", prefix)
        for candidate in reversed(candidates):
            if _is_plausible_name(candidate):
                return candidate

    scrubbed = text.replace(phone, " ") if phone else text
    tokens = re.split(r"[\s,，;；、]+", scrubbed)
    for token in tokens:
        token = re.sub(r"^(?:姓名|收件人|寄件人|联系人)[:：]?", "", token)
        if any(item and item in token for item in location_variants):
            continue
        if _is_plausible_name(token):
            return token

    if phone:
        idx = text.find(phone)
        if idx > 0:
            prefix = text[max(0, idx - 15):idx]
            chunks = re.findall(r"[\u4e00-\u9fa5]{2,6}", prefix)
            for chunk in reversed(chunks):
                if _is_plausible_name(chunk):
                    return chunk
    return None

def _match_province_city_district(text):
    divisions = _load_divisions()
    province_node = _find_node(text, divisions, "province")

    if not province_node:
        for alias in sorted(PROVINCE_ALIASES, key=len, reverse=True):
            if alias in text:
                full_name = PROVINCE_ALIASES[alias]
                province_node = next(
                    (item for item in divisions if item.get("name") == full_name), None
                )
                if province_node:
                    break

    city_node = None
    if province_node:
        city_node = _find_node(text, _cities_of(province_node), "city")
        if not city_node and province_node.get("name") in DIRECT_MUNICIPALITIES:
            cities = _cities_of(province_node)
            city_node = cities[0] if cities else None
    else:
        city_matches = []
        for candidate_province in divisions:
            candidate_city = _find_node(text, _cities_of(candidate_province), "city")
            if candidate_city:
                full_name = candidate_city.get("name") or ""
                position = min(
                    (text.find(v) for v in _name_variants(full_name, "city") if v in text),
                    default=len(text),
                )
                city_matches.append((position, -len(full_name), candidate_province, candidate_city))
        if city_matches:
            _, _, province_node, city_node = min(city_matches, key=lambda item: item[:2])

    district_node = _find_node(
        text, (city_node or {}).get("children") or [], "district"
    )

    if province_node and not city_node:
        for candidate_city in _cities_of(province_node):
            candidate_district = _find_node(
                text, candidate_city.get("children") or [], "district"
            )
            if candidate_district:
                city_node, district_node = candidate_city, candidate_district
                break

    return (
        (province_node or {}).get("name", ""),
        (city_node or {}).get("name", ""),
        (district_node or {}).get("name", ""),
    )


def _matching_nodes(text, nodes, level):
    """Return every node explicitly mentioned by full name or a safe suffix-free alias."""
    matched = []
    for node in nodes or []:
        full_name = node.get("name") or ""
        variants = list(_name_variants(full_name, level))
        if level == "district":
            variants.extend(HISTORICAL_DIVISION_ALIASES.get(full_name, ()))
        variants = sorted(set(variants), key=len, reverse=True)
        hit = None
        for variant in variants:
            position = text.find(variant)
            if position < 0:
                continue
            if variant != full_name:
                # District abbreviations are too collision-prone nationwide.  City/province
                # abbreviations are allowed, except when they are plainly part of a street
                # name such as “中山路” rather than the city “中山市”.
                historical_aliases = HISTORICAL_DIVISION_ALIASES.get(full_name, ())
                if level == "district" and variant not in historical_aliases:
                    continue
                following = text[position + len(variant):position + len(variant) + 2]
                if following.startswith(("路", "街", "巷", "道", "号", "村", "镇", "乡", "园")):
                    continue
            hit = variant
            break
        if hit:
            matched.append((node, hit, text.find(hit)))
    return matched


def _resolve_address_divisions(text):
    """Resolve an address-only string without guessing between duplicate districts."""
    divisions = _load_divisions()
    province_hits = _matching_nodes(text, divisions, "province")
    province_nodes = [item[0] for item in province_hits]

    city_paths = []
    district_paths = []
    for province in divisions:
        for city in _cities_of(province):
            for _, city_hit, city_pos in _matching_nodes(text, [city], "city"):
                city_paths.append((province, city, None, city_hit, city_pos))
            for district, district_hit, district_pos in _matching_nodes(
                text, city.get("children") or [], "district"
            ):
                district_paths.append((province, city, district, district_hit, district_pos))

    def under_explicit_province(paths):
        if not province_nodes:
            return paths
        allowed = {node.get("name") for node in province_nodes}
        return [path for path in paths if path[0].get("name") in allowed]

    city_paths = under_explicit_province(city_paths)
    district_paths = under_explicit_province(district_paths)
    explicit_city_names = {path[1].get("name") for path in city_paths}
    if explicit_city_names:
        narrowed = [path for path in district_paths if path[1].get("name") in explicit_city_names]
        if narrowed:
            district_paths = narrowed

    unique_district_paths = {
        (path[0].get("name"), path[1].get("name"), path[2].get("name")): path
        for path in district_paths
    }
    if len(unique_district_paths) == 1:
        province, city, district, _, _ = next(iter(unique_district_paths.values()))
        return province.get("name", ""), city.get("name", ""), district.get("name", ""), ""
    if len(unique_district_paths) > 1:
        names = sorted({f"{path[0].get('name')}{path[1].get('name')}" for path in unique_district_paths.values()})
        return "", "", "", f"区县名称存在歧义，请补充城市（可能属于：{'、'.join(names[:4])}）"

    unique_city_paths = {
        (path[0].get("name"), path[1].get("name")): path for path in city_paths
    }
    if len(unique_city_paths) == 1:
        province, city, _, _, _ = next(iter(unique_city_paths.values()))
        return province.get("name", ""), city.get("name", ""), "", "未识别到区县"
    if len(unique_city_paths) > 1:
        names = sorted({f"{path[0].get('name')}{path[1].get('name')}" for path in unique_city_paths.values()})
        return "", "", "", f"城市名称存在歧义，请补充省份（可能属于：{'、'.join(names[:4])}）"

    if len(province_nodes) == 1:
        return province_nodes[0].get("name", ""), "", "", "未识别到城市和区县"
    return "", "", "", "未识别到有效的省市区"

def _extract_detail(text, province, city, district, name, phone):
    detail = text
    m = ADDR_LABEL_RE.search(detail)
    if m: detail = detail[m.end():]
    for label in ["姓名","收件人","寄件人","联系人","手机","电话"]:
        detail = re.sub(rf"{label}[\s:：]*", " ", detail)
    location_variants = [province, city, district]
    location_variants.extend(
        alias for alias, full_name in PROVINCE_ALIASES.items() if full_name == province
    )
    for full_name, level in [(city, "city"), (district, "district")]:
        if full_name:
            location_variants.extend(_name_variants(full_name, level))
    for x in [*location_variants, name, phone]:
        if x: detail = detail.replace(x, " ")
    detail = detail.replace("，"," ").replace(","," ").replace("；"," ").replace(";"," ").replace("、"," ")
    detail = re.sub(r"^[\s:：;；,，、]+", "", detail)
    detail = re.sub(r"\s+", " ", detail).strip()
    return detail


def _extract_address_detail(text, province, city, district):
    """Remove one leading occurrence of each administrative division only."""
    detail = text.strip()
    label = ADDR_LABEL_RE.search(detail)
    if label:
        detail = detail[label.end():]
    seen = set()
    for full_name, level in ((province, "province"), (city, "city"), (district, "district")):
        if not full_name or full_name in seen:
            continue
        seen.add(full_name)
        variants = list(_name_variants(full_name, level))
        if level == "district":
            variants.extend(HISTORICAL_DIVISION_ALIASES.get(full_name, ()))
        for variant in sorted(set(variants), key=len, reverse=True):
            match = re.search(re.escape(variant), detail)
            if match and match.start() <= 4:
                detail = detail[:match.start()] + detail[match.end():]
                detail = re.sub(r"^[\s,，;；、:：]+", "", detail)
                break
    return re.sub(r"\s+", " ", detail).strip()

def parse_recipient(text):
    if not text or not text.strip():
        return {"success": False, "message": "文本为空"}
    text = text.strip()
    phone = _match_phone(text)
    province, city, district = _match_province_city_district(text)
    name = _match_name(text, phone, province, city, district)
    address = _extract_detail(text, province, city, district, name, phone)
    score = 0.0
    if phone: score += 0.4
    if name: score += 0.25
    if province: score += 0.15
    if city: score += 0.1
    if address and len(address) >= 5: score += 0.1
    return {
        "success": bool(phone or name or address),
        "name": name or "", "phone": phone or "",
        "province": province, "city": city, "district": district,
        "address": address, "raw": text,
        "confidence": round(min(score, 1.0), 2),
    }


def parse_address(text):
    """Parse one address-only cell into canonical province/city/district/detail fields."""
    raw = str(text or "").strip()
    if not raw:
        return {"success": False, "message": "地址为空", "raw": raw}
    normalized = re.sub(r"[\t\r\n]+", " ", raw)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    province, city, district, error = _resolve_address_divisions(normalized)
    detail = _extract_address_detail(normalized, province, city, district)
    if error:
        return {
            "success": False, "message": error, "province": province, "city": city,
            "district": district, "address": detail, "raw": raw,
        }
    if len(detail) < 3:
        return {
            "success": False, "message": "详细地址过短，请填写街道、楼栋或门牌号",
            "province": province, "city": city, "district": district,
            "address": detail, "raw": raw,
        }
    return {
        "success": True, "message": "地址识别成功", "province": province,
        "city": city, "district": district, "address": detail, "raw": raw,
        "confidence": 1.0 if province in normalized and city in normalized else 0.9,
    }
