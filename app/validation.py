"""订阅方与事件配置校验。非法配置直接抛 InvalidSubscriberError / InvalidEventError。"""
from __future__ import annotations

from urllib.parse import urlparse

from app.errors import InvalidEventError, InvalidSubscriberError

ALLOWED_URL_SCHEMES = {"http", "https"}
MIN_SECRET_LENGTH = 8
MAX_SECRET_LENGTH = 256
MAX_URL_LENGTH = 2048
MAX_EVENT_TYPE_LENGTH = 128


def validate_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise InvalidSubscriberError("url 不能为空")
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise InvalidSubscriberError(f"url 长度不能超过 {MAX_URL_LENGTH}")
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_URL_SCHEMES:
        raise InvalidSubscriberError(f"url scheme 必须是 {sorted(ALLOWED_URL_SCHEMES)}，收到: {parsed.scheme!r}")
    if not parsed.hostname:
        raise InvalidSubscriberError("url 缺少主机名")
    # 凭据不允许写在 URL 里（必须走独立 secret）
    if parsed.username or parsed.password:
        raise InvalidSubscriberError("url 中不允许携带用户名/密码，请使用 secret 字段")
    return url


def validate_secret(secret: str) -> str:
    if not isinstance(secret, str) or not secret:
        raise InvalidSubscriberError("secret 不能为空")
    if len(secret) < MIN_SECRET_LENGTH:
        raise InvalidSubscriberError(f"secret 长度至少 {MIN_SECRET_LENGTH} 个字符")
    if len(secret) > MAX_SECRET_LENGTH:
        raise InvalidSubscriberError(f"secret 长度不能超过 {MAX_SECRET_LENGTH}")
    if any(ch.isspace() for ch in secret):
        raise InvalidSubscriberError("secret 不能包含空白字符")
    return secret


def validate_event_types(event_types) -> list[str]:
    if not isinstance(event_types, list) or not event_types:
        raise InvalidSubscriberError("event_types 必须是非空数组")
    result: list[str] = []
    seen: set[str] = set()
    for et in event_types:
        if not isinstance(et, str) or not et.strip():
            raise InvalidSubscriberError("event_types 中每一项都必须是非空字符串")
        et = et.strip()
        if len(et) > MAX_EVENT_TYPE_LENGTH:
            raise InvalidSubscriberError(f"event_type 长度不能超过 {MAX_EVENT_TYPE_LENGTH}")
        if et in seen:
            raise InvalidSubscriberError(f"event_types 存在重复项: {et}")
        seen.add(et)
        result.append(et)
    return result


def validate_event(event_type: str, payload: dict) -> None:
    if not isinstance(event_type, str) or not event_type.strip():
        raise InvalidEventError("event_type 必须是非空字符串")
    if not isinstance(payload, dict):
        raise InvalidEventError("payload 必须是 JSON 对象")
