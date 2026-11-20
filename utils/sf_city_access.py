"""Regional access control for SF City shop-mode delivery."""
from __future__ import annotations

from fastapi import HTTPException

from database import models
from utils.regions import BEIJING, XIAMEN_OTHER, get_config, normalize_region


SF_CITY_REGIONS = {XIAMEN_OTHER, BEIJING}
REGION_CITY_KEYWORD = {XIAMEN_OTHER: "厦门", BEIJING: "北京"}


def _enabled(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def sf_city_region_enabled(db, region_code: str) -> bool:
    """Return whether one region has an enabled, isolated SF City shop."""
    from config.settings import settings

    region = normalize_region(region_code)
    if region not in SF_CITY_REGIONS:
        return False
    # The public preview exposes the real SF City pages with synthetic shop
    # data. Live ordering remains disabled by PREVIEW_SAFE_MODE.
    if settings.preview_safe_mode:
        return True
    live = get_config(db, region, "sf_city_live_order_enabled", "")
    shop_id = get_config(db, region, "sf_city_shop_id", "")
    if live or shop_id:
        return _enabled(live) and bool(shop_id)
    # Legacy env settings belong to the original Xiamen rollout only. Never
    # let Beijing silently fall back to the Xiamen shop and settlement account.
    return bool(
        region == XIAMEN_OTHER
        and (settings.sf_city_feature_enabled or settings.sf_city_live_order_enabled)
        and settings.sf_city_shop_id
    )


def sf_city_runtime_enabled(db) -> bool:
    """Return whether SF City is active in at least one supported region."""
    return any(sf_city_region_enabled(db, region) for region in SF_CITY_REGIONS)


def sf_city_user_allowed(
    db, current_user, *, auth_enabled: bool = True, region_code: str = XIAMEN_OTHER,
) -> bool:
    """Check that the employee and requested shop belong to the same region."""
    region = normalize_region(region_code)
    if region not in SF_CITY_REGIONS:
        return False
    if not auth_enabled:
        return True
    user_id = (current_user or {}).get("id")
    if not user_id:
        return False
    user = db.query(models.User).filter(models.User.id == int(user_id)).first()
    if not user or not bool(getattr(user, "is_active", True)):
        return False
    if normalize_region(getattr(user, "region_code", None)) != region:
        return False
    location_text = " ".join((
        str(getattr(user, "work_location", None) or ""),
        str(getattr(user, "department", None) or ""),
    ))
    return REGION_CITY_KEYWORD[region] in location_text


def require_sf_city_user(
    db, current_user, *, region_code: str, auth_enabled: bool = True,
) -> None:
    region = normalize_region(region_code)
    if not sf_city_region_enabled(db, region):
        raise HTTPException(status_code=404, detail="当前区域尚未启用顺丰同城急送")
    if not sf_city_user_allowed(
        db, current_user, auth_enabled=auth_enabled, region_code=region,
    ):
        raise HTTPException(status_code=403, detail="顺丰同城急送仅对已开通区域的本地员工开放")


def require_sf_city_xiamen_user(db, current_user, *, auth_enabled: bool = True) -> None:
    """Compatibility wrapper retained for old internal imports and tests."""
    require_sf_city_user(
        db, current_user, region_code=XIAMEN_OTHER, auth_enabled=auth_enabled,
    )
