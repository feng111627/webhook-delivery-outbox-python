"""FastAPI 路由：订阅方管理、事件提交、投递状态查询。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from webhook.errors import NotFoundError, ValidationError


class RegisterSubscriptionIn(BaseModel):
    name: str = Field(..., description="订阅方名称，唯一")
    url: str = Field(..., description="回调地址，仅支持 http/https")
    secret: str = Field(..., description="HMAC-SHA256 密钥，至少 16 字节")
    event_types: list[str] = Field(..., description="关心的事件类型列表")


class SetEnabledIn(BaseModel):
    enabled: bool


class SubmitEventIn(BaseModel):
    event_id: str = Field(..., description="业务侧幂等键，相同 event_id 只投递一次")
    event_type: str
    payload: dict


def _raise_http(exc: Exception, status_code: int) -> None:
    raise HTTPException(status_code=status_code, detail=str(exc))


def create_router(service) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.post("/subscriptions", status_code=201)
    def register_subscription(body: RegisterSubscriptionIn):
        try:
            return service.register_subscription(
                body.name, body.url, body.secret, body.event_types
            )
        except ValidationError as exc:
            _raise_http(exc, 422)

    @router.get("/subscriptions")
    def list_subscriptions():
        return {"subscriptions": service.list_subscriptions()}

    @router.get("/subscriptions/{subscription_id}")
    def get_subscription(subscription_id: int):
        try:
            return service.get_subscription(subscription_id)
        except NotFoundError as exc:
            _raise_http(exc, 404)

    @router.post("/subscriptions/{subscription_id}/enabled")
    def set_enabled(subscription_id: int, body: SetEnabledIn):
        try:
            return service.set_subscription_enabled(subscription_id, body.enabled)
        except NotFoundError as exc:
            _raise_http(exc, 404)

    @router.post("/events")
    def submit_event(body: SubmitEventIn, response: Response):
        try:
            result = service.submit_event(body.event_id, body.event_type, body.payload)
            # 重复提交是正常幂等行为，返回 200；首次提交返回 202
            response.status_code = 200 if result["deduped"] else 202
            return result
        except ValidationError as exc:
            _raise_http(exc, 422)

    @router.get("/events/{event_id}")
    def get_event(event_id: str):
        try:
            return service.get_event_status(event_id)
        except NotFoundError as exc:
            _raise_http(exc, 404)

    @router.get("/deliveries/{delivery_id}")
    def get_delivery(delivery_id: int):
        try:
            return service.get_delivery_detail(delivery_id)
        except NotFoundError as exc:
            _raise_http(exc, 404)

    return router
