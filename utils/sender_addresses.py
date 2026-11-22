"""Enterprise sender-address policy shared by pages and order APIs."""
from fastapi import HTTPException

from database import models
from utils.regions import CHENGDU, XIAMEN_OTHER, normalize_region, user_region


DEFAULT_COMPANY_SENDER_ADDRESSES = (
    (XIAMEN_OTHER, "厦门示例办公室", "示例科技有限公司", "福建省", "厦门市", "思明区",
     "示例大道100号A座601室"),
    (CHENGDU, "成都示例办公室", "示例科技有限公司", "四川省", "成都市", "成华区",
     "示例路200号B座101室"),
)
SEED_MARKER = "company_sender_addresses_seeded"


def ensure_company_sender_addresses(db):
    marker = db.query(models.SystemConfig).filter(
        models.SystemConfig.config_key == SEED_MARKER
    ).first()
    if marker and str(marker.config_value or "").strip() == "2":
        return
    # 旧版已初始化过厦门地址时，只增补新的成都默认地址；
    # 标记升级到 2 后不再自动恢复被管理员删除的默认地址。
    defaults = (
        DEFAULT_COMPANY_SENDER_ADDRESSES
        if not marker
        else tuple(row for row in DEFAULT_COMPANY_SENDER_ADDRESSES if row[0] == CHENGDU)
    )
    for region, label, company, province, city, district, detail in defaults:
        exists = db.query(models.Address).filter(
            models.Address.scope == "company", models.Address.role == "sender",
            models.Address.region_code == region, models.Address.label == label,
        ).first()
        if not exists:
            db.add(models.Address(
                scope="company", role="sender", label=label, name="", phone="",
                region_code=region,
                company_name=company, province=province, city=city,
                district=district, detail=detail,
            ))
    if not marker:
        db.add(models.SystemConfig(
            config_key=SEED_MARKER, config_value="2",
            description="分区域企业寄件地址默认数据已初始化",
        ))
    else:
        marker.config_value = "2"
    db.commit()


def user_can_custom_sender_address(db, current_user):
    if not current_user:
        return True
    if current_user.get("role") == "admin":
        return True
    user = db.query(models.User).filter(models.User.id == int(current_user["id"])).first()
    return bool(user and user.allow_custom_sender_address)


def _normalized(value):
    return "".join(str(value or "").split())


def sender_matches_company_address(db, data, region_code=XIAMEN_OTHER):
    ensure_company_sender_addresses(db)
    addresses = db.query(models.Address).filter(
        models.Address.scope == "company", models.Address.role == "sender"
        , models.Address.region_code == normalize_region(region_code)
    ).all()
    expected = tuple(_normalized(data.get(key)) for key in (
        "sender_company", "sender_province", "sender_city", "sender_district", "sender_address"
    ))
    return any(expected == tuple(_normalized(value) for value in (
        address.company_name, address.province, address.city, address.district, address.detail
    )) for address in addresses)


def assert_sender_address_allowed(db, current_user, data):
    if user_can_custom_sender_address(db, current_user):
        return
    if not sender_matches_company_address(db, data, user_region(db, current_user)):
        raise HTTPException(403, "当前账号只能使用管理员维护的企业寄件地址")
