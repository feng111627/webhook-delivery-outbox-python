"""领域相关异常。"""
from __future__ import annotations


class WebhookError(Exception):
    """所有业务异常的基类。"""


class InvalidSubscriberError(WebhookError):
    """订阅方配置不合法（URL/密钥/事件类型等）。"""


class NotFoundError(WebhookError):
    """资源不存在。"""


class InvalidEventError(WebhookError):
    """事件内容不合法。"""
