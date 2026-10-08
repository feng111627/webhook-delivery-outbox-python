"""Webhook 签名生成与校验。

签名协议（与接收端约定一致，详见 docs/protocol.md）：

* 时间戳头 ``X-Webhook-Timestamp``：Unix 秒（UTC 整数）。
* 签名头 ``X-Webhook-Signature``：``sha256=<hex HMAC>``。
* 被签名的字符串为 ``f"{timestamp}.{body}"``，其中 body 是实际发送的
  JSON 字节串；密钥使用订阅方各自的 secret，算法 HMAC-SHA256。
* 接收端应校验时间戳与当前时间的偏差不超过约定窗口（默认 300 秒），
  以防重放。
"""
from __future__ import annotations

import hashlib
import hmac
import time


def build_signing_payload(timestamp: str, body: bytes) -> bytes:
    """组装被签名内容：``<timestamp>.<body>``。"""
    if not isinstance(timestamp, str):
        raise TypeError("timestamp must be a str of unix seconds")
    return timestamp.encode("ascii") + b"." + body


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """生成 ``sha256=<hexdigest>`` 形式的签名。"""
    mac = hmac.new(secret.encode("utf-8"), build_signing_payload(timestamp, body), hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    """常数时间比较校验签名。格式必须为 ``sha256=<hex>``。"""
    expected = sign(secret, timestamp, body)
    return hmac.compare_digest(expected, signature or "")


def timestamp_is_fresh(
    timestamp: str,
    max_skew_seconds: int = 300,
    now: float | None = None,
) -> bool:
    """时间戳是否在允许偏差窗口内。非法时间戳一律视为不新鲜。"""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    return abs(current - ts) <= max_skew_seconds
