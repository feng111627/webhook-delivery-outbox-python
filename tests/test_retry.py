import random

import pytest

from webhook.retry import backoff_delay


def test_exponential_backoff_sequence():
    assert [backoff_delay(i, 1, 60) for i in range(1, 7)] == [1, 2, 4, 8, 16, 32]


def test_backoff_is_capped():
    assert backoff_delay(20, 1, 60) == 60


def test_jitter_stays_in_range():
    rng = random.Random(42)
    for _ in range(50):
        d = backoff_delay(3, 1, 60, jitter=True, rng=rng)
        assert 0 <= d <= 4


def test_invalid_attempt():
    with pytest.raises(ValueError):
        backoff_delay(0)
