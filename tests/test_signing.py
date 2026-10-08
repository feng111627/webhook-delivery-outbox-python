import time

from webhook.signing import sign, timestamp_is_fresh, verify


def test_sign_and_verify_roundtrip():
    body = b'{"event_id":"e1"}'
    sig = sign("0123456789abcdef", "1700000000", body)
    assert sig.startswith("sha256=")
    assert len(sig) == len("sha256=") + 64
    assert verify("0123456789abcdef", "1700000000", body, sig)


def test_verify_rejects_tampering():
    body = b'{"x":1}'
    sig = sign("0123456789abcdef", "1700000000", body)
    assert not verify("other-secret-9876543210ab", "1700000000", body, sig)
    assert not verify("0123456789abcdef", "1700000001", body, sig)
    assert not verify("0123456789abcdef", "1700000000", b'{"x":2}', sig)
    assert not verify("0123456789abcdef", "1700000000", body, "sha256=deadbeef")
    assert not verify("0123456789abcdef", "1700000000", body, "")


def test_timestamp_freshness_window():
    now = 1_700_000_000
    assert timestamp_is_fresh(str(now), 300, now=float(now))
    assert timestamp_is_fresh(str(now - 300), 300, now=float(now))
    assert not timestamp_is_fresh(str(now - 301), 300, now=float(now))
    assert not timestamp_is_fresh("not-a-timestamp", 300, now=float(now))
