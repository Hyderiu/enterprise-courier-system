"""Shared outbound HTTP pool for courier gateways.

The production server makes repeated HTTPS calls while DingTalk users fill the
order form. Reusing TCP/TLS connections avoids paying the handshake cost on
every quote refresh. Request-specific authentication remains on each call.
"""
import requests
from requests.adapters import HTTPAdapter


def build_http_session(pool_size: int = 32) -> requests.Session:
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=pool_size,
        pool_maxsize=pool_size,
        max_retries=0,
        pool_block=False,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


COURIER_HTTP_SESSION = build_http_session()
_ORIGINAL_REQUESTS_POST = requests.post


def pooled_post(*args, **kwargs):
    """Use the pool in production while honoring tests that patch requests.post."""
    if requests.post is not _ORIGINAL_REQUESTS_POST:
        return requests.post(*args, **kwargs)
    return COURIER_HTTP_SESSION.post(*args, **kwargs)
