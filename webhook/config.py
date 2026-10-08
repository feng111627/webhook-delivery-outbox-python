"""本地配置加载（JSON 文件，所有地址/密钥均为本地模拟值）。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: str = "data/webhook.db"
    poll_interval: float = 0.1
    http_timeout: float = 5.0
    max_attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 60.0
    timestamp_skew_seconds: int = 300


_DEFAULTS = Settings()


def load_settings(path: str | Path | None = None) -> Settings:
    """从 JSON 文件加载设置；文件不存在或为 None 时返回默认值。

    只接受已知字段，拼错的字段直接报错而不是被静默忽略。
    """
    if path is None:
        return _DEFAULTS
    p = Path(path)
    if not p.exists():
        return _DEFAULTS
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"settings file {path} must contain a JSON object")
    allowed = set(asdict(_DEFAULTS))
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unknown settings keys: {sorted(unknown)}")
    return Settings(**raw)
