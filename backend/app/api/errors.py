"""API 层统一错误：所有业务错误抛 ApiError，由 main.py 的异常处理器
转成统一结构 `{detail: {code, message}}`，前端按 code 提示，不解析文案。"""
from __future__ import annotations


class ApiError(Exception):
    """业务错误。status 为 HTTP 状态码，code 为前端可判定的错误码。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class NotFoundError(ApiError):
    def __init__(self, code: str, message: str):
        super().__init__(404, code, message)


class BadRequestError(ApiError):
    def __init__(self, code: str, message: str):
        super().__init__(400, code, message)


class ConflictError(ApiError):
    def __init__(self, code: str, message: str):
        super().__init__(409, code, message)
