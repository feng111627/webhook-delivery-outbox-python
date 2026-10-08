"""统一错误类型。"""
from __future__ import annotations


class WebhookError(Exception):
    """所有业务错误的基类。"""


class ValidationError(WebhookError):
    """订阅方/事件配置非法。"""


class NotFoundError(WebhookError):
    """资源不存在。"""


class ConflictError(WebhookError):
    """状态冲突（例如重复提交被去重不算错误）。"""
