"""签名生成与校验测试。"""
from __future__ import annotations

from app.signing import sign, verify


def test_sign_and_verify_roundtrip():
    sig = sign("s3cr3t-key", 1_700_000_000, "evt-1", '{"amount":100}')
    assert sig.startswith("sha256=")
    assert verify("s3cr3t-key", 1_700_000_000, "evt-1", '{"amount":100}', sig)


def test_tampered_body_fails():
    sig = sign("s3cr3t-key", 1_700_000_000, "evt-1", '{"amount":100}')
    assert not verify("s3cr3t-key", 1_700_000_000, "evt-1", '{"amount":101}', sig)


def test_tampered_event_or_timestamp_fails():
    sig = sign("s3cr3t-key", 1_700_000_000, "evt-1", "{}")
    assert not verify("s3cr3t-key", 1_700_000_000, "evt-2", "{}", sig)
    assert not verify("s3cr3t-key", 1_700_000_001, "evt-1", "{}", sig)
    assert not verify("other-key", 1_700_000_000, "evt-1", "{}", sig)


def test_deterministic_known_vector():
    # 固定向量：HMAC_SHA256("1700000000.evt-1.{}", key="abc12345")
    import hashlib
    import hmac

    expected = hmac.new(b"abc12345", b"1700000000.evt-1.{}", hashlib.sha256).hexdigest()
    assert sign("abc12345", 1_700_000_000, "evt-1", "{}") == f"sha256={expected}"


def test_empty_signature_rejected():
    assert not verify("k", 1, "e", "{}", "")
