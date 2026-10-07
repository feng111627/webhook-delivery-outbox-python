"""本地配置：全部来自环境变量或默认值，不依赖任何外部服务。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


@dataclass(frozen=True)
class Settings:
    database_path: str = "data/webhooks.db"
    max_attempts: int = 5
    base_backoff_seconds: float = 0.2
    max_backoff_seconds: float = 30.0
    request_timeout_seconds: float = 5.0
    lock_seconds: float = 30.0
    poll_interval_seconds: float = 0.1
    signature_tolerance_seconds: int = 300
    worker_threads: int = 8

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            database_path=os.environ.get("WEBHOOK_DB_PATH", "data/webhooks.db"),
            max_attempts=_env_int("WEBHOOK_MAX_ATTEMPTS", 5),
            base_backoff_seconds=_env_float("WEBHOOK_BASE_BACKOFF", 0.2),
            max_backoff_seconds=_env_float("WEBHOOK_MAX_BACKOFF", 30.0),
            request_timeout_seconds=_env_float("WEBHOOK_TIMEOUT", 5.0),
            lock_seconds=_env_float("WEBHOOK_LOCK_SECONDS", 30.0),
            poll_interval_seconds=_env_float("WEBHOOK_POLL_INTERVAL", 0.1),
            signature_tolerance_seconds=_env_int("WEBHOOK_TOLERANCE", 300),
            worker_threads=_env_int("WEBHOOK_WORKER_THREADS", 8),
        )
