"""Short-lived signed login links used only in private DingTalk work notices."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from config.settings import settings
from utils.session_secret import get_session_secret


NOTIFICATION_LINK_TTL_SECONDS = 7 * 24 * 60 * 60


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_notification_login_token(
    user_id: int,
    dingtalk_userid: str,
    target_path: str,
    *,
    now: int | None = None,
) -> str:
    target = str(target_path or "/")
    if not target.startswith("/") or target.startswith("//"):
        target = "/"
    issued_at = int(time.time() if now is None else now)
    payload = {
        "purpose": "dingtalk-work-notice",
        "uid": int(user_id),
        "did": str(dingtalk_userid or "").strip(),
        "target": target,
        "exp": issued_at + NOTIFICATION_LINK_TTL_SECONDS,
    }
    body = _encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    secret = get_session_secret(settings.session_secret).encode("utf-8")
    signature = _encode(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def verify_notification_login_token(token: str, *, now: int | None = None) -> dict:
    try:
        body, signature = str(token or "").split(".", 1)
        secret = get_session_secret(settings.session_secret).encode("utf-8")
        expected = _encode(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid signature")
        payload = json.loads(_decode(body).decode("utf-8"))
        current_time = int(time.time() if now is None else now)
        target = str(payload.get("target") or "")
        if payload.get("purpose") != "dingtalk-work-notice":
            raise ValueError("invalid purpose")
        if int(payload.get("exp") or 0) < current_time:
            raise ValueError("expired")
        if not int(payload.get("uid") or 0) or not str(payload.get("did") or "").strip():
            raise ValueError("invalid user")
        if not target.startswith("/") or target.startswith("//"):
            raise ValueError("invalid target")
        return payload
    except (TypeError, ValueError, KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("无效或已过期的工作提醒链接") from exc
