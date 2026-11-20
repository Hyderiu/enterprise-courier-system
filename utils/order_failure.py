"""Extract a safe, concise carrier failure from a persisted order response."""
from __future__ import annotations

import json
from typing import Any


MESSAGE_KEYS = (
    "subMsg", "message", "msg", "errorMessage", "errorMsg", "error_msg",
    "resultMessage", "resultMsg", "failReason", "failureReason", "reason",
    "errorDescription", "errorDetail", "detail", "description", "error",
)
CODE_KEYS = (
    "subCode", "errorCode", "error_code", "code", "resultCode", "apiResultCode",
)

SUCCESS_CODES = {"0", "200", "1000", "0000", "S0000", "A1000"}
GENERIC_MESSAGES = {
    "失败", "处理失败", "业务处理失败", "请求失败", "下单失败",
    "京东物流业务处理失败", "承运商正式下单失败", "系统异常", "未知异常",
}


def _first_text(value: Any, keys: tuple[str, ...]) -> str:
    if isinstance(value, dict):
        for key in keys:
            found = value.get(key)
            if found is not None and not isinstance(found, (dict, list)):
                text = str(found).strip()
                if text:
                    return text
        for child in value.values():
            found = _first_text(child, keys)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_text(child, keys)
            if found:
                return found
    return ""


def _collect_texts(value: Any, keys: tuple[str, ...], depth: int = 0) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and child is not None and not isinstance(child, (dict, list)):
                text = str(child).strip()
                if text:
                    found.append((depth, text))
            found.extend(_collect_texts(child, keys, depth + 1))
    elif isinstance(value, list):
        for child in value:
            found.extend(_collect_texts(child, keys, depth + 1))
    elif isinstance(value, str) and value.strip().startswith(("{", "[")):
        try:
            found.extend(_collect_texts(json.loads(value), keys, depth + 1))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return found


def _specific_message(value: Any) -> str:
    candidates = _collect_texts(value, MESSAGE_KEYS)
    candidates = [item for item in candidates if item[1].lower() not in {"success", "ok", "成功"}]
    if not candidates:
        return ""

    def score(item: tuple[int, str]) -> tuple[int, int, int]:
        depth, text = item
        generic = text.strip("：:。. ") in GENERIC_MESSAGES
        # 京东网关外层常只写“业务处理失败”，真正原因位于更深层。
        return (0 if generic else 1, depth, min(len(text), 200))

    return max(candidates, key=score)[1]


def _specific_code(value: Any) -> str:
    candidates = _collect_texts(value, CODE_KEYS)
    failures = [item for item in candidates if item[1].upper() not in SUCCESS_CODES]
    selected = max(failures or candidates, key=lambda item: item[0], default=(0, ""))
    return selected[1]


def extract_order_failure(order_response: Any) -> dict[str, str]:
    """Return only an error code/message; never expose the full carrier payload."""
    if order_response in (None, ""):
        return {"code": "", "message": "正式下单未成功，承运商没有返回具体原因"}
    value = order_response
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"code": "", "message": value.strip()[:500]}
    code = _specific_code(value)
    message = _specific_message(value)
    if not message:
        message = "正式下单未成功，承运商没有返回具体原因"
    return {"code": code[:100], "message": message[:500]}
