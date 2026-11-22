"""Small dependency-free signed-cookie session middleware."""
from __future__ import annotations

import base64
from http.cookies import SimpleCookie
import hashlib
import hmac
import json
import time

from starlette.datastructures import Headers, MutableHeaders


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class SignedCookieSessionMiddleware:
    def __init__(self, app, secret_key: str, session_cookie: str = "session",
                 max_age: int = 43200, same_site: str = "lax", https_only: bool = True):
        self.app = app
        self.secret = secret_key.encode("utf-8")
        self.cookie = session_cookie
        self.max_age = max_age
        self.same_site = same_site
        self.https_only = https_only

    def _decode(self, token: str) -> dict:
        try:
            payload_text, signature = token.rsplit(".", 1)
            expected = _b64encode(hmac.new(self.secret, payload_text.encode("ascii"), hashlib.sha256).digest())
            if not hmac.compare_digest(signature, expected):
                return {}
            payload = json.loads(_b64decode(payload_text))
            if time.time() - float(payload.get("iat", 0)) > self.max_age:
                return {}
            data = payload.get("data")
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _encode(self, session: dict) -> str:
        payload = _b64encode(json.dumps(
            {"iat": int(time.time()), "data": session}, ensure_ascii=False,
            separators=(",", ":")).encode("utf-8"))
        signature = _b64encode(hmac.new(self.secret, payload.encode("ascii"), hashlib.sha256).digest())
        return payload + "." + signature

    def _cookie_header(self, value: str, max_age: int) -> str:
        cookie = SimpleCookie()
        cookie[self.cookie] = value
        morsel = cookie[self.cookie]
        morsel["path"] = "/"
        morsel["max-age"] = str(max_age)
        morsel["httponly"] = True
        morsel["samesite"] = self.same_site
        if self.https_only:
            morsel["secure"] = True
        return morsel.OutputString()

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        raw_cookie = Headers(scope=scope).get("cookie", "")
        parsed = SimpleCookie()
        parsed.load(raw_cookie)
        token = parsed[self.cookie].value if self.cookie in parsed else ""
        scope["session"] = self._decode(token) if token else {}
        initially_empty = not scope["session"]

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if scope["session"]:
                    headers.append("set-cookie", self._cookie_header(self._encode(scope["session"]), self.max_age))
                elif not initially_empty:
                    headers.append("set-cookie", self._cookie_header("", 0))
            await send(message)

        await self.app(scope, receive, send_wrapper)
