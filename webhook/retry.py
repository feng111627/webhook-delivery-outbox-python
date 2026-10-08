"""退避重试策略：指数退避 + 抖动上限封顶。"""
from __future__ import annotations

import random


def backoff_delay(
    attempt: int,
    base: float = 1.0,
    maximum: float = 60.0,
    jitter: bool = False,
    rng: random.Random | None = None,
) -> float:
    """第 ``attempt`` 次投递失败后需要等待的秒数（attempt 从 1 开始）。

    公式：``min(maximum, base * 2 ** (attempt - 1))``，可选全抖动
    （在 [0, delay] 间均匀取值），避免大量订阅方同时重试造成雷鸣群。
    """
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    delay = min(maximum, base * (2 ** (attempt - 1)))
    if jitter:
        source = rng or random
        delay = source.uniform(0.0, delay)
    return max(0.0, delay)
