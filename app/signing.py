"""HMAC-SHA256 签名生成与校验。

签名规则（README 中同样有说明）：

    signed_payload = f"{timestamp}.{event_id}.{body}"
    signature      = HMAC_SHA256(secret, signed_payload) 的十六进制摘要

HTTP 头：

    X-Webhook-Event:     事件业务 ID
    X-Webhook-Timestamp: Unix 秒级时间戳
    X-Webhook-Signature: sha256=<十六进制摘要>
"""
from __future__ import annotations

import hashlib
import hmac

HEADER_EVENT = "X-Webhook-Event"
HEADER_TIMESTAMP = "X-Webhook-Timestamp"
HEADER_SIGNATURE = "X-Webhook-Signature"

SIGNATURE_SCHEME = "sha256"


def build_signed_payload(timestamp: int, event_id: str, body: str) -> bytes:
    """构造规范待签名串: "<unix_ts>.<event_id>.<raw body>"。"""
    return f"{timestamp}.{event_id}.{body}".encode("utf-8")


def sign(secret: str, timestamp: int, event_id: str, body: str) -> str:
    """生成形如 "sha256=<hex>" 的签名头值。"""
    mac = hmac.new(secret.encode("utf-8"), build_signed_payload(timestamp, event_id, body), hashlib.sha256)
    return f"{SIGNATURE_SCHEME}={mac.hexdigest()}"


def _raw_signature(header_value: str) -> str:
    if header_value.startswith(f"{SIGNATURE_SCHEME}="):
        return header_value[len(SIGNATURE_SCHEME) + 1 :]
    return header_value


def verify(secret: str, timestamp: int, event_id: str, body: str, signature: str) -> bool:
    """恒定时间比较签名是否匹配。"""
    if not signature:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), build_signed_payload(timestamp, event_id, body), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, _raw_signature(signature))
