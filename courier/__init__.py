from .base import CourierClient
from sqlalchemy import or_

from .sf import SFCourier
from .deppon import DepponCourier
from .jdl import JDLCourier
from .sf_city import SFCityCourier

COURIER_REGISTRY = {
    "SF": SFCourier, "DB": DepponCourier, "JDL": JDLCourier, "JD": JDLCourier,
    "SF_CITY": SFCityCourier,
}

def _db_config(db, key: str, region_code: str | None = None):
    if db is None:
        return None
    if region_code:
        from utils.regions import region_key
        from database import models
        row = db.query(models.SystemConfig).filter(
            models.SystemConfig.config_key == region_key(region_code, key)
        ).first()
        if row and row.config_value is not None:
            return row.config_value.strip()
        # 已启用分区配置后，缺失值必须保持为空，不能回落到环境变量中的其他区域账号。
        marker = db.query(models.SystemConfig).filter(or_(
            models.SystemConfig.config_key == region_key(region_code, "enabled_couriers"),
            models.SystemConfig.config_key.like(f"region.{region_code}.%"),
        )).first()
        return "" if marker else None
    from database import models
    row = db.query(models.SystemConfig).filter(models.SystemConfig.config_key == key).first()
    return row.config_value.strip() if row and row.config_value is not None else None


def get_courier(courier_type: str, db=None, region_code: str | None = None) -> CourierClient:
    cls = COURIER_REGISTRY.get(courier_type.upper())
    if not cls:
        raise ValueError(f"Unknown courier type: {courier_type}")
    if courier_type.upper() == "SF":
        return cls(
            partner_id=_db_config(db, "sf_customer_code", region_code),
            check_word=_db_config(db, "sf_check_word", region_code),
            monthly_card=_db_config(db, "sf_monthly_card", region_code),
        )
    if courier_type.upper() == "SF_CITY":
        from config.settings import settings
        return cls(
            host=settings.sf_city_api_url,
            dev_id=_db_config(db, "sf_city_dev_id", region_code) or settings.sf_city_dev_id,
            dev_key=_db_config(db, "sf_city_dev_key", region_code) or settings.sf_city_dev_key,
            shop_id=_db_config(db, "sf_city_shop_id", region_code) or settings.sf_city_shop_id,
            timeout=settings.sf_api_timeout,
            live_order_enabled=(
                not settings.preview_safe_mode
                and str(
                    _db_config(db, "sf_city_live_order_enabled", region_code)
                    or settings.sf_city_live_order_enabled
                ).strip().lower() in {"1", "true", "yes", "on"}
            ),
        )
    if courier_type.upper() in {"JDL", "JD"}:
        return cls(
            app_key=_db_config(db, "jdl_app_key", region_code),
            app_secret=_db_config(db, "jdl_app_secret", region_code),
            access_token=_db_config(db, "jdl_access_token", region_code),
            customer_code=_db_config(db, "jdl_customer_code", region_code),
            business_unit_code=_db_config(db, "jdl_business_unit_code", region_code),
        )
    return cls(
        company_code=_db_config(db, "db_company_code", region_code),
        customer_code=_db_config(db, "db_customer_code", region_code),
        appkey=_db_config(db, "db_appkey", region_code),
        sign_value=_db_config(db, "db_sign", region_code),
    )


def get_courier_for_order(order, db=None) -> CourierClient:
    client = get_courier(order.courier_type, db, getattr(order, "region_code", None))
    snapshot = str(getattr(order, "billing_account_code", None) or "").strip()
    if snapshot:
        if order.courier_type.upper() == "SF":
            client.monthly_card = snapshot
        elif order.courier_type.upper() == "SF_CITY":
            # Keep historical Xiamen/Beijing orders on the shop captured when
            # the order was created, even if the regional config changes later.
            client.shop_id = snapshot
        else:
            client.customer_code = snapshot
    return client
