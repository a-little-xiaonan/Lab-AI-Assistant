"""请求上下文：统一生成 request_id，供响应、日志和审计关联。"""
from __future__ import annotations

import re
import logging
from contextvars import ContextVar
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

_REQUEST_ID = ContextVar("request_id", default=None)
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{8,64}$")


def current_request_id() -> str | None:
    return _REQUEST_ID.get()


class RequestIdFilter(logging.Filter):
    """为日志记录补 request_id；后台任务没有请求上下文时使用短横线。"""
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id() or "-"
        return True


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request_id = supplied if _VALID_REQUEST_ID.fullmatch(supplied) else uuid4().hex
        token = _REQUEST_ID.set(request_id)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            _REQUEST_ID.reset(token)
