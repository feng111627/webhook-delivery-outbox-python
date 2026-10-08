"""订阅方配置校验：非法地址/密钥在入口处明确拒绝。"""
from __future__ import annotations

from urllib.parse import urlsplit

from webhook.errors import ValidationError

_ALLOWED_SCHEMES = {"http", "https"}
_MIN_SECRET_BYTES = 16
_MAX_SECRET_BYTES = 256
_MAX_URL_LENGTH = 2048


def validate_url(url: str) -> str:
    """URL 必须是 http/https、带主机名；本地模拟用 http://127.0.0.1:<port>。"""
    if not isinstance(url, str) or not url.strip():
        raise ValidationError("url 不能为空")
    url = url.strip()
    if len(url) > _MAX_URL_LENGTH:
        raise ValidationError(f"url 过长（>{_MAX_URL_LENGTH}）")
    parts = urlsplit(url)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValidationError("url 只支持 http/https 协议")
    if not parts.hostname:
        raise ValidationError("url 缺少主机名")
    if parts.scheme.lower() == "https" and parts.username:
        raise ValidationError("url 不允许在地址中内嵌用户名")
    return url


def validate_secret(secret: str) -> str:
    """密钥必须是长度 16~256 字节的非空字符串，不能是明显占位值。"""
    if not isinstance(secret, str) or not secret:
        raise ValidationError("secret 不能为空")
    n = len(secret.encode("utf-8"))
    if n < _MIN_SECRET_BYTES:
        raise ValidationError(f"secret 至少 {_MIN_SECRET_BYTES} 字节（当前 {n}）")
    if n > _MAX_SECRET_BYTES:
        raise ValidationError(f"secret 不能超过 {_MAX_SECRET_BYTES} 字节")
    if secret.strip() != secret:
        raise ValidationError("secret 首尾不能有空白字符")
    weak = {"0" * 16, "secretsecret1234", "changeme12345678"}
    if secret in weak:
        raise ValidationError("secret 不能使用明显的占位弱值")
    return secret


def validate_event_types(event_types) -> list[str]:
    """事件类型列表：非空、元素为非空字符串、去重保序。"""
    if not isinstance(event_types, list) or not event_types:
        raise ValidationError("event_types 必须是非空数组")
    seen: set[str] = set()
    result: list[str] = []
    for item in event_types:
        if not isinstance(item, str) or not item.strip():
            raise ValidationError("event_types 中的每一项都必须是非空字符串")
        name = item.strip()
        if any(ch.isspace() for ch in name):
            raise ValidationError(f"事件类型不能包含空白: {name!r}")
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("订阅方名称不能为空")
    name = name.strip()
    if len(name) > 128:
        raise ValidationError("订阅方名称不能超过 128 字符")
    return name
