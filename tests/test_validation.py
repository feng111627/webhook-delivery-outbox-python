import pytest

from webhook.errors import ValidationError
from webhook.validation import (
    validate_event_types,
    validate_name,
    validate_secret,
    validate_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9100/hook",
        "https://example.com/webhook",
        "  http://localhost/x  ",
    ],
)
def test_valid_urls(url):
    assert validate_url(url) == url.strip()


@pytest.mark.parametrize(
    "url",
    ["", "ftp://x/y", "http://", "https:///x", "javascript:alert(1)", "not a url"],
)
def test_invalid_urls(url):
    with pytest.raises(ValidationError):
        validate_url(url)


@pytest.mark.parametrize("secret", ["0123456789abcdef", "a" * 256])
def test_valid_secret(secret):
    assert validate_secret(secret) == secret


@pytest.mark.parametrize(
    "secret",
    [
        "",
        "short",
        "0123456789abcde",  # 15 字节
        " 0123456789abcdef",
        "secretsecret1234",
        "a" * 257,
    ],
)
def test_invalid_secret(secret):
    with pytest.raises(ValidationError):
        validate_secret(secret)


def test_event_types_dedup_and_validation():
    assert validate_event_types(["a", "b", "a"]) == ["a", "b"]
    with pytest.raises(ValidationError):
        validate_event_types([])
    with pytest.raises(ValidationError):
        validate_event_types(["ok", ""])
    with pytest.raises(ValidationError):
        validate_event_types(["has space"])


def test_name_validation():
    assert validate_name("  x  ") == "x"
    with pytest.raises(ValidationError):
        validate_name("   ")
