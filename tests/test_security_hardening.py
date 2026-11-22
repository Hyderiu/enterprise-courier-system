import hashlib
import hmac
import time
import base64
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.main import app
from api.auth_middleware import PUBLIC_EXACT
from api.routes.courier import _parse_sf_route_xml
from api.routes.dingtalk_webhook import _verify_signature
from config.settings import settings
from database.connection import Base, get_db
from database import models
from utils.dingtalk_notification_auth import create_notification_login_token


class CallbackSecurityTests(unittest.TestCase):
    def setUp(self):
        self.original = {
            "dingtalk_login_enabled": settings.dingtalk_login_enabled,
            "dingtalk_event_token": settings.dingtalk_event_token,
            "sf_check_word": settings.sf_check_word,
        }
        settings.dingtalk_login_enabled = False
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        factory = sessionmaker(bind=self.engine)

        def override_db():
            db = factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        for key, value in self.original.items():
            setattr(settings, key, value)
        self.engine.dispose()

    def test_dingtalk_signature_is_mandatory(self):
        settings.dingtalk_event_token = ""
        self.assertFalse(_verify_signature("1", "n", "{}", "anything"))
        response = self.client.post("/api/dingtalk/event", content=b"{}")
        self.assertEqual(response.status_code, 503)

        settings.dingtalk_event_token = "secret"
        response = self.client.post("/api/dingtalk/event", content=b"{}")
        self.assertEqual(response.status_code, 401)
        body = "{}"
        timestamp = str(int(time.time()))
        sort_str = "".join(sorted(["secret", timestamp, "n", body]))
        signature = hmac.new(b"secret", sort_str.encode(), hashlib.sha256).hexdigest()
        response = self.client.post(
            f"/api/dingtalk/event?timestamp={timestamp}&nonce=n&signature={signature}", content=body
        )
        self.assertEqual(response.status_code, 200)

    def test_sf_route_push_fails_closed_and_accepts_verified_empty_payload(self):
        settings.sf_check_word = ""
        self.assertEqual(
            self.client.post("/api/courier/sf/route-push", json={}).status_code, 503
        )
        db = self.engine.connect()
        db.execute(models.SystemConfig.__table__.insert().values(
            config_key="sf_check_word", config_value="existing-check-word"
        ))
        db.commit()
        db.close()
        self.assertEqual(
            self.client.post("/api/courier/sf/route-push", json={}).status_code, 401
        )
        content = '{"WaybillRoute":[]}'
        verify_code = base64.b64encode(
            hashlib.md5((content + "existing-check-word").encode()).digest()
        ).decode()
        response = self.client.post(
            "/api/courier/sf/route-push",
            content=content,
            headers={"Content-Type": "application/json", "X-SF-Verify-Code": verify_code},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["return_code"], "0000")

    def test_legacy_mutating_callbacks_are_retired(self):
        self.assertNotIn("/api/dingtalk/oa-approval", PUBLIC_EXACT)
        self.assertNotIn("/api/dingtalk/bitable-sync", PUBLIC_EXACT)
        self.assertEqual(self.client.post("/api/dingtalk/oa-approval", json={}).status_code, 410)
        self.assertEqual(self.client.post("/api/dingtalk/bitable-sync", json={}).status_code, 404)

    def test_private_work_notice_link_logs_in_exact_recipient_and_redirects(self):
        with sessionmaker(bind=self.engine)() as db:
            user = models.User(
                name="审批人", dingtalk_userid="approver-1", role="employee", is_active=True
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            token = create_notification_login_token(
                user.id, user.dingtalk_userid, "/admin/approvals?order_id=18"
            )
        response = self.client.get(
            "/dingtalk-notification-entry", params={"token": token}, follow_redirects=False
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/admin/approvals?order_id=18")
        self.assertIn("courier_session=", response.headers.get("set-cookie", ""))
        rejected = self.client.get(
            "/dingtalk-notification-entry", params={"token": token + "x"},
            follow_redirects=False,
        )
        self.assertEqual(rejected.status_code, 401)

    def test_sf_xml_content_is_normalized(self):
        parsed = _parse_sf_route_xml(
            '<Request><Body><WaybillRoute mailno="SF1" orderid="CO1" opCode="80" />'
            '</Body></Request>'
        )
        self.assertEqual(parsed["WaybillRoute"][0]["mailno"], "SF1")

    def test_html_pages_disable_embedded_browser_cache_and_show_real_version(self):
        response = self.client.get("/express")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers.get("cache-control", ""))
        version = self.client.get("/api/version").json()["version"]
        self.assertEqual(response.headers.get("x-courier-version"), version)
        self.assertIn(f"v{version}", response.text)


class SqliteConfigurationTests(unittest.TestCase):
    def test_foreign_keys_and_busy_timeout_are_enabled(self):
        from database.connection import engine

        with engine.connect() as connection:
            self.assertEqual(connection.execute(text("PRAGMA foreign_keys")).scalar(), 1)
            self.assertGreaterEqual(connection.execute(text("PRAGMA busy_timeout")).scalar(), 30000)


if __name__ == "__main__":
    unittest.main()
