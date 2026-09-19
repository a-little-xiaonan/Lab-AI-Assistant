"""工具平台基础设施：声明、策略、执行与可验证的结果登记。

工具必须由代码预注册；模型只能从当前请求允许的声明中选择，不能加载任意插件或执行代码。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, ValidationError

from app.config import settings
from app.core.agent.models import ToolContext, ToolError, ToolResult
from app.core.agent.runtime import run_bounded

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolPolicy:
    data_classification: str  # public / internal / sensitive
    adapter: str  # local / mcp / http；M2 仅使用 local
    enabled: Callable[[], bool] = lambda: True
    max_argument_bytes: int = 16_000
    cacheable: bool = True


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    args: type[BaseModel]
    handler: Callable
    policy: ToolPolicy
    version: str = "v1"

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.args.model_json_schema(),
        }}


class ToolPlatform:
    def __init__(self, definitions: list[ToolDefinition]) -> None:
        self._definitions = {definition.name: definition for definition in definitions}
        if len(self._definitions) != len(definitions):
            raise ValueError("tool names must be unique")

    @property
    def definitions(self) -> dict[str, ToolDefinition]:
        return dict(self._definitions)

    def schemas(self) -> list[dict]:
        return [tool.schema() for tool in self._definitions.values() if tool.policy.enabled()]

    def execute(self, call: dict, ctx: ToolContext) -> ToolResult:
        started = time.monotonic()
        name, call_id = call["function"]["name"], call["id"]

        def failure(code: str, message: str, status: str = "failed") -> ToolResult:
            return ToolResult(call_id=call_id, tool=name, status=status,
                              error=ToolError(code=code, message=message))

        if ctx.calls >= settings.agent_max_tool_calls:
            return failure("call_budget_exhausted", "已达到本次工具调用上限。")
        ctx.calls += 1
        try:
            ctx.budget.remaining()
            definition = self._definitions.get(name)
            if definition is None:
                return failure("unknown_tool", "该能力尚未注册。", "unsupported")
            if not definition.policy.enabled():
                return failure("tool_disabled", "该能力暂未启用。", "unsupported")
            raw = call["function"]["arguments"]
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > definition.policy.max_argument_bytes:
                return failure("invalid_arguments", "工具参数不符合要求，请补充或更正。", "needs_input")
            args = definition.args.model_validate_json(raw)
            key = (definition.name, definition.version,
                   json.dumps(args.model_dump(mode="json"), sort_keys=True, ensure_ascii=False))
            if definition.policy.cacheable and key in ctx.cache:
                result = ctx.cache[key].model_copy(deep=True)
                result.cache_hit = True
            else:
                result = run_bounded(ctx.budget, definition.handler, args, ctx)
            if result.status == "ok" and not result.evidence_refs:
                ref = f"evidence_{len(ctx.evidence_ledger) + 1}"
                result.evidence_refs = [ref]
                ctx.evidence_ledger[ref] = {
                    "tool": definition.name, "version": definition.version,
                    "classification": definition.policy.data_classification,
                    "data": result.data, "provenance": result.provenance,
                }
            if definition.policy.cacheable and key not in ctx.cache:
                # 缓存必须包含已分配的证据引用，重复调用才可追溯到同一事实来源。
                ctx.cache[key] = result.model_copy(deep=True)
            result.call_id, result.tool = call_id, name
            result.duration_ms = round((time.monotonic() - started) * 1000)
            logger.info("tool request=%s name=%s version=%s adapter=%s status=%s duration_ms=%d cache_hit=%s",
                        ctx.request_id, name, definition.version, definition.policy.adapter,
                        result.status, result.duration_ms, result.cache_hit)
            return result
        except ValidationError:
            return failure("invalid_arguments", "工具参数不符合要求，请补充或更正。", "needs_input")
        except TimeoutError:
            return failure("prepare_timeout", "本次准备已超时，仅能提供已取得的结果。")
        except Exception:
            logger.exception("工具执行失败：name=%s", name)
            return failure("tool_execution_failed", "工具暂时无法执行，请稍后重试。")
