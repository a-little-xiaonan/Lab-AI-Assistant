"""LLM 层错误定义：上层（API 层）只捕获 LLMError，SDK 原始异常不泄漏到接口。"""
from __future__ import annotations


class LLMError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
