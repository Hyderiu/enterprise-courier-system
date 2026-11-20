from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import models
from database.connection import get_db
from utils.sender_addresses import ensure_company_sender_addresses
from utils.regions import normalize_region


router = APIRouter()


class CompanyAddressBody(BaseModel):
    region_code: str = "xiamen_other"
    label: str
    company_name: str = ""
    province: str
    city: str
    district: str
    detail: str


def _serialize(address):
    return {
        "id": address.id, "label": address.label or "企业寄件地址",
        "region_code": address.region_code or "xiamen_other",
        "company_name": address.company_name or "", "province": address.province or "",
        "city": address.city or "", "district": address.district or "",
        "detail": address.detail or "",
    }


def _clean(body):
    data = {key: str(value or "").strip() for key, value in body.model_dump().items()}
    missing = [label for key, label in (
        ("label", "地址名称"), ("province", "省"), ("city", "市"),
        ("district", "区县"), ("detail", "详细地址"),
    ) if not data[key]]
    if missing:
        raise HTTPException(400, "请填写" + "、".join(missing))
    return data


@router.get("/company-addresses")
def list_company_addresses(region: str = "xiamen_other", db: Session = Depends(get_db)):
    ensure_company_sender_addresses(db)
    rows = db.query(models.Address).filter(
        models.Address.scope == "company", models.Address.role == "sender",
        models.Address.region_code == normalize_region(region),
    ).order_by(models.Address.id).all()
    return {"ok": True, "items": [_serialize(row) for row in rows]}


@router.post("/company-addresses")
def create_company_address(body: CompanyAddressBody, db: Session = Depends(get_db)):
    data = _clean(body)
    data["region_code"] = normalize_region(data["region_code"])
    row = models.Address(
        scope="company", role="sender", name="", phone="", **data,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"ok": True, "item": _serialize(row)}


@router.put("/company-addresses/{address_id}")
def update_company_address(
    address_id: int, body: CompanyAddressBody, db: Session = Depends(get_db),
):
    row = db.query(models.Address).filter(
        models.Address.id == address_id, models.Address.scope == "company",
        models.Address.role == "sender",
    ).first()
    if not row:
        raise HTTPException(404, "企业寄件地址不存在")
    for key, value in _clean(body).items():
        setattr(row, key, normalize_region(value) if key == "region_code" else value)
    db.commit()
    db.refresh(row)
    return {"ok": True, "item": _serialize(row)}


@router.delete("/company-addresses/{address_id}")
def delete_company_address(address_id: int, db: Session = Depends(get_db)):
    row = db.query(models.Address).filter(
        models.Address.id == address_id, models.Address.scope == "company",
        models.Address.role == "sender",
    ).first()
    if not row:
        raise HTTPException(404, "企业寄件地址不存在")
    db.delete(row)
    db.commit()
    return {"ok": True}
