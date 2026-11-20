import json
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import models
from database.connection import Base
from utils.regions import (
    BEIJING, CHENGDU, XIAMEN_OTHER, default_service_id, enabled_carriers, get_config,
    ensure_default_service_catalog, region_from_work_location, request_region, save_config,
    save_enabled_carriers, user_region,
)
from utils.sender_addresses import ensure_company_sender_addresses


class RegionFrameworkTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_work_location_mapping(self):
        self.assertEqual(region_from_work_location("成都办公室"), CHENGDU)
        self.assertEqual(region_from_work_location("北京总部"), BEIJING)
        self.assertEqual(region_from_work_location("厦门软件园"), XIAMEN_OTHER)
        self.assertEqual(region_from_work_location("上海外地"), XIAMEN_OTHER)
        self.assertEqual(region_from_work_location(""), XIAMEN_OTHER)

    def test_carrier_and_account_settings_are_isolated_by_region(self):
        save_config(self.db, XIAMEN_OTHER, "sf_monthly_card", "XM-SF")
        save_config(self.db, CHENGDU, "jdl_customer_code", "CD-JD")
        save_enabled_carriers(self.db, XIAMEN_OTHER, ["SF", "DB"])
        save_enabled_carriers(self.db, CHENGDU, ["JDL"])
        save_enabled_carriers(self.db, BEIJING, [])
        self.db.commit()

        self.assertEqual(get_config(self.db, XIAMEN_OTHER, "sf_monthly_card"), "XM-SF")
        self.assertEqual(get_config(self.db, CHENGDU, "sf_monthly_card"), "")
        self.assertEqual(get_config(self.db, CHENGDU, "jdl_customer_code"), "CD-JD")
        self.assertEqual(enabled_carriers(self.db, XIAMEN_OTHER), ["SF", "DB"])
        self.assertEqual(enabled_carriers(self.db, CHENGDU), ["JDL"])
        self.assertEqual(enabled_carriers(self.db, BEIJING), [])

    def test_default_catalog_migration_adds_beijing_deppon_only_once(self):
        save_enabled_carriers(self.db, BEIJING, ["SF", "JDL"])
        self.db.commit()
        self.assertTrue(ensure_default_service_catalog(self.db))
        self.db.commit()
        self.assertEqual(enabled_carriers(self.db, BEIJING), ["SF", "DB", "JDL"])
        self.assertFalse(ensure_default_service_catalog(self.db))

    def test_sf_city_historical_order_keeps_its_captured_shop(self):
        from courier import get_courier_for_order

        for key, value in (
            ("sf_city_dev_id", "1770000000"),
            ("sf_city_dev_key", "secret"),
            ("sf_city_shop_id", "CURRENT-BEIJING-SHOP"),
            ("sf_city_live_order_enabled", "1"),
        ):
            save_config(self.db, BEIJING, key, value)
        self.db.commit()
        order = SimpleNamespace(
            courier_type="SF_CITY", region_code=BEIJING,
            billing_account_code="ORDER-CAPTURED-BEIJING-SHOP",
        )
        client = get_courier_for_order(order, self.db)
        self.assertEqual(client.shop_id, "ORDER-CAPTURED-BEIJING-SHOP")

    def test_user_region_comes_from_persisted_employee_location(self):
        user = models.User(
            name="成都员工", dingtalk_userid="cd-user",
            work_location="成都办公室", region_code=CHENGDU,
        )
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        self.assertEqual(user_region(self.db, {"id": user.id}), CHENGDU)
        self.assertEqual(request_region(self.db, {"id": user.id}, BEIJING, True), CHENGDU)
        self.assertEqual(request_region(self.db, {"id": user.id}, BEIJING, False), BEIJING)

    def test_debug_order_page_exposes_three_regions_and_correct_service_catalogs(self):
        from fastapi.testclient import TestClient
        from api.main import app
        from config.settings import settings
        from database.connection import get_db

        factory = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        original_login = settings.dingtalk_login_enabled
        settings.dingtalk_login_enabled = False

        def override_db():
            db = factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_db
        client = TestClient(app)
        try:
            chengdu_page = client.get("/express?region=chengdu")
            self.assertEqual(chengdu_page.status_code, 200)
            self.assertIn("调试预览区域", chengdu_page.text)
            for label in ("厦门及其他", "成都", "北京"):
                self.assertIn(label, chengdu_page.text)
            self.assertIn("/static/jdl-logo.svg", chengdu_page.text)
            self.assertNotIn("顺丰即日", chengdu_page.text)

            chengdu = client.get("/api/services?region=chengdu").json()
            self.assertEqual(chengdu["region_code"], CHENGDU)
            self.assertEqual(chengdu["default_service_id"], "jdl_biaokuai")
            self.assertEqual(chengdu["default_service_name"], "京东标快")
            self.assertEqual(
                [(row["name"], row["express_type"]) for row in chengdu["items"]],
                [("京东标快", "ed-m-0001"), ("京东特快", "ed-m-0002")],
            )
            self.assertTrue(all(row["supports_insurance"] for row in chengdu["items"]))
            self.assertTrue(all(row["supports_multi_parcel"] for row in chengdu["items"]))

            xiamen_result = client.get("/api/services?region=xiamen_other").json()
            xiamen = xiamen_result["items"]
            self.assertEqual(xiamen_result["default_service_id"], "db_small_standard")
            self.assertEqual(xiamen_result["default_service_name"], "德邦小件标快")
            self.assertEqual({row["courier"] for row in xiamen}, {"SF", "DB"})
            self.assertNotIn("sf_same_day", {row["id"] for row in xiamen})
            self.assertEqual(client.get("/api/services?region=beijing").json()["items"], [])

            save_enabled_carriers(self.db, BEIJING, ["SF", "DB", "JDL"])
            self.db.commit()
            beijing_result = client.get("/api/services?region=beijing").json()
            beijing = beijing_result["items"]
            self.assertEqual(beijing_result["default_service_id"], "db_small_standard")
            self.assertEqual(
                [(row["name"], row["express_type"]) for row in beijing],
                [
                    ("顺丰特快", "1"), ("顺丰标快", "2"),
                    ("顺丰大件", "255"),
                    ("大件特快", "DJTK"), ("小件特快", "XJTK"),
                    ("大件标快", "DJBK"), ("小件标快", "XJBK"),
                    ("京东标快", "ed-m-0001"), ("京东特快", "ed-m-0002"),
                ],
            )

            saved = client.post("/api/admin/default-service", json={
                "region_code": BEIJING, "service_id": "sf_biaokuai",
            })
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(default_service_id(self.db, BEIJING), "sf_biaokuai")
            admin_page = client.get("/admin/item-types")
            self.assertIn("各地区默认推荐", admin_page.text)
            self.assertIn("德邦 · 小件标快", admin_page.text)
        finally:
            app.dependency_overrides.pop(get_db, None)
            settings.dingtalk_login_enabled = original_login

    def test_default_company_addresses_are_regional_and_seed_only_once(self):
        ensure_company_sender_addresses(self.db)
        xiamen = self.db.query(models.Address).filter_by(
            scope="company", role="sender", region_code=XIAMEN_OTHER,
        ).all()
        chengdu = self.db.query(models.Address).filter_by(
            scope="company", role="sender", region_code=CHENGDU,
        ).all()
        beijing = self.db.query(models.Address).filter_by(
            scope="company", role="sender", region_code=BEIJING,
        ).all()
        self.assertEqual([row.label for row in xiamen], ["厦门示例办公室"])
        self.assertEqual([row.label for row in chengdu], ["成都示例办公室"])
        self.assertEqual(beijing, [])

        removed_id = xiamen[0].id
        self.db.delete(xiamen[0])
        self.db.commit()
        ensure_company_sender_addresses(self.db)
        self.assertIsNone(self.db.query(models.Address).filter_by(id=removed_id).first())


if __name__ == "__main__":
    unittest.main()
