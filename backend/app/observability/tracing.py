"""请求内阶段记录与线程安全关联；只接受明确允许的元数据。

P4 起按 retrieval / model / tool 分类开关；默认不记录任何正文、Prompt、
工具内容或鉴权材料。列表字段只允许稳定 ID 与枚举，并在入队前截断。
"""
from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from uuid import uuid4

from app.config import settings
from app.middleware.request_context import current_request_id
from app.observability.exporter import get_exporter

logger = logging.getLogger(__name__)

PIPELINE_VERSION = "intent-history-p4"
SCHEMA_VERSION = "chat-trace-v1"

_CURRENT: ContextVar[Trace | None] = ContextVar("chat_trace", default=None)
_PARENT: ContextVar[tuple[str, str] | None] = ContextVar("chat_span", default=None)
_FIELDS = frozenset({
    # 请求与回合
    "stream", "http_status", "transport_complete", "disconnected", "assistant_saved",
    "transport_status", "business_outcome", "outcome", "error_code", "reason",
    "history_revision", "history_turns", "history_dropped_turns", "turn_status",
    "lease_acquired", "incomplete", "late_result", "assistant_message_id",
    # 规划与来源
    "route", "parts_count", "handler", "role", "answer_kind", "basis_status",
    "plan_version", "action", "basis", "reference_message_ids", "target_message_ids",
    "target_count", "clarification_reason", "resolved_question_chars",
    "derived_from_message_ids", "derivation_depth", "provenance_exists",
    "permission_projected", "transform_kind",
    # 历史与上下文
    "query_chars", "history_chars", "history_before_chars", "history_messages",
    "history_dropped_messages", "history_summary_present", "history_message_ids",
    "answer_ref_count", "pending_clarification",
    "readable_kb_count", "kb_id", "kb_ids", "part_id", "subquery_id",
    # 检索
    "candidate_ids", "candidate_count", "candidate_ranks", "excluded_count",
    "previous_candidate_ids",
    "truncated", "executed", "skip_reason", "fallback_reason", "query_count",
    "subquery_count", "rewrite_count", "vector_similarity", "bm25_score",
    "rrf_score", "rerank_score", "threshold", "accepted_count", "source_count",
    "tool_source_count", "answer_mode", "path_count", "top_k", "mode",
    # 证据门槛
    "evidence_ids", "evidence_count", "judge_used", "fail_open", "fail_closed",
    "accepted", "gate_reason", "supporting_chunks", "total_chunks",
    "max_similarity", "max_lexical_coverage", "max_rerank_score",
    # 工具
    "tool", "tool_version", "adapter", "call_id", "cache_hit", "arg_bytes",
    "tool_count", "tool_names", "round",
    # 模型与 Prompt
    "purpose", "model", "provider_request_id", "attempt", "max_attempts",
    "retry_count", "first_text_ms", "input_tokens", "output_tokens",
    "cached_tokens", "cache_creation_tokens", "total_tokens", "usage_present",
    "template_version", "prompt_fingerprint", "message_roles", "message_count",
    "input_chars", "output_chars", "budget_tokens", "trimmed",
    "processor_version", "normalized",
    # 导出与容量
    "dropped_events", "write_failures", "disk_capped", "export_incomplete",
    "elapsed_ms",
})
_MAX_STRING = 160
_MAX_LIST = 50
# 白名单之外的纵深防御：即使未来误把字段加入白名单，也不落正文/凭据。
_SENSITIVE_KEYS = frozenset({
    "prompt", "question", "answer", "content", "text", "query", "history",
    "snippet", "body", "authorization", "cookie", "api_key", "token", "secret",
    "url", "html", "system_prompt", "model_output",
})
_CATEGORY_SWITCHES = {
    "retrieval": "trace_retrieval_enabled",
    "model": "trace_model_enabled",
    "tool": "trace_tool_enabled",
}


def _safe_value(value):
    """只保留可序列化且不含正文的标量/ID 列表；其他类型整字段丢弃。"""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:_MAX_STRING]
    if isinstance(value, (list, tuple)):
        items = []
        for item in value[:_MAX_LIST]:
            if isinstance(item, bool):
                items.append(item)
            elif isinstance(item, int):
                items.append(item)
            elif isinstance(item, float):
                if math.isfinite(item):
                    items.append(item)
            elif isinstance(item, str):
                items.append(item[:_MAX_STRING])
        return items
    return None


def _safe(values: dict) -> dict:
    safe = {}
    for key, value in values.items():
        if key not in _FIELDS or key.casefold() in _SENSITIVE_KEYS:
            continue
        projected = _safe_value(value)
        if projected is not None or value is None:
            safe[key] = projected
    return safe


def bounded_ids(values, limit: int | None = None) -> list:
    """候选/证据 ID 统一截断；调用方可同时记录 truncated 属性。"""
    if not values:
        return []
    maximum = limit if limit is not None else settings.trace_candidate_limit
    return list(values)[: max(1, maximum)]


def is_truncated(values, limit: int | None = None) -> bool:
    maximum = limit if limit is not None else settings.trace_candidate_limit
    return bool(values) and len(values) > max(1, maximum)


def category_enabled(category: str) -> bool:
    field = _CATEGORY_SWITCHES.get(category)
    return bool(getattr(settings, field, True)) if field else True


def exception_status(exc: BaseException) -> str:
    return "cancelled" if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) else "error"


class Span:
    def __init__(self, trace, name: str, parent: str, attributes: dict) -> None:
        self.trace, self.name, self.parent = trace, name, parent
        self.id = uuid4().hex
        self.started = time.monotonic()
        self.attributes = _safe(attributes)
        self.ended = False

    def set_attributes(self, **attributes) -> None:
        if self.trace:
            with self.trace.lock:
                self.attributes.update(_safe(attributes))

    def finish(self, status: str = "ok", **attributes) -> None:
        if not self.trace:
            return
        with self.trace.lock:
            if self.ended:
                return
            self.ended = True
            self.attributes.update(_safe(attributes))
            self.trace.active.pop(self.id, None)
            self.trace.emit(self.name, "end", self.id, self.parent, status,
                            round((time.monotonic() - self.started) * 1000, 3), self.attributes)

    @contextmanager
    def activate(self):
        token = _PARENT.set((self.trace.id, self.id)) if self.trace else None
        try:
            yield self
        finally:
            if token is not None:
                _PARENT.reset(token)

    def __enter__(self):
        self._activation = self.activate()
        self._activation.__enter__()
        return self

    def __exit__(self, kind, exc, tb):
        try:
            if exc is None:
                self.finish()
            else:
                self.finish(exception_status(exc), error_code=getattr(exc, "code", type(exc).__name__))
        finally:
            self._activation.__exit__(kind, exc, tb)


class Trace:
    def __init__(self, exporter, request_id: str | None) -> None:
        self.exporter = exporter
        self.id, self.root_id = uuid4().hex, uuid4().hex
        self.request_id = request_id
        self.session_id = self.message_id = self.turn_id = None
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.active: dict[str, Span] = {}
        self.sequence = 0
        self.closed = self.saved = self.first_delta = self.disconnected = False
        self.error_code = None
        self.attributes = {}
        self.emit("chat.request", "start", self.root_id, None)

    def emit(self, stage, event, span_id, parent_id, status=None, duration_ms=None, attributes=None):
        with self.lock:
            self.sequence += 1
            record = {
                "schema_version": SCHEMA_VERSION, "pipeline_version": PIPELINE_VERSION,
                "timestamp": datetime.now(timezone.utc).isoformat(), "trace_id": self.id,
                "request_id": self.request_id, "session_id": self.session_id,
                "turn_id": self.turn_id, "message_id": self.message_id, "sequence": self.sequence,
                "span_id": span_id, "parent_span_id": parent_id,
                "stage": stage, "event": event, "status": status,
                "duration_ms": duration_ms, "late_result": self.closed,
                "attributes": _safe(attributes or {}),
            }
            try:
                self.exporter.emit(record)
            except Exception:
                # 自定义导出器也不能把记录失败传播到业务流程。
                if not getattr(self, "_export_error_reported", False):
                    self._export_error_reported = True
                    logger.warning("聊天追踪导出器异常，本轮诊断可能不完整")

    def start_span(self, name: str, **attributes) -> Span:
        with self.lock:
            parent = _PARENT.get()
            parent_id = parent[1] if parent and parent[0] == self.id else self.root_id
            span = Span(self, name, parent_id, attributes)
            if not self.closed:
                self.active[span.id] = span
            self.emit(name, "start", span.id, parent_id, attributes=span.attributes)
            return span

    def finish(self, status: str, **attributes) -> None:
        with self.lock:
            if self.closed:
                return
            for span in list(self.active.values()):
                span.finish("cancelled" if status == "cancelled" else "degraded",
                            reason="request_finished_before_stage")
            try:
                stats = self.exporter.stats()
                capacity = getattr(self.exporter, "capacity_stats", None)
                if capacity is not None:
                    stats = {**stats, **capacity()}
            except Exception:
                stats = {}
            stats["export_incomplete"] = bool(
                stats.get("dropped_events") or stats.get("write_failures") or stats.get("disk_capped")
            )
            stats["incomplete"] = stats["export_incomplete"]
            final = {**self.attributes, **stats, **attributes, "assistant_saved": self.saved,
                     "disconnected": self.disconnected}
            if self.error_code:
                final["error_code"] = self.error_code
            self.emit("chat.request", "end", self.root_id, None, status,
                      round((time.monotonic() - self.started) * 1000, 3), final)
            self.closed = True


def current_trace() -> Trace | None:
    return _CURRENT.get()


def new_trace() -> Trace | None:
    exporter = get_exporter()
    return Trace(exporter, current_request_id()) if settings.observability_enabled and exporter else None


@contextmanager
def bind_trace(trace: Trace):
    token = _CURRENT.set(trace)
    parent = _PARENT.set((trace.id, trace.root_id))
    try:
        yield trace
    finally:
        _PARENT.reset(parent)
        _CURRENT.reset(token)


def stage(name: str, **attributes) -> Span:
    trace = current_trace()
    return trace.start_span(name, **attributes) if trace else Span(None, name, "", {})


def stage_for(category: str, name: str, **attributes) -> Span:
    """按 retrieval/model/tool 分类开关记录阶段；关闭时返回空 Span。"""
    if not category_enabled(category):
        return Span(None, name, "", {})
    return stage(name, **attributes)


def bind_session(session_id: str, **attributes) -> None:
    if trace := current_trace():
        with trace.lock:
            if not trace.closed:
                trace.session_id = session_id
                trace.attributes.update(_safe(attributes))


def bind_turn(turn_id: str, **attributes) -> None:
    if trace := current_trace():
        with trace.lock:
            if not trace.closed:
                trace.turn_id = turn_id
                trace.attributes.update(_safe(attributes))


def mark_error(code: str) -> None:
    if trace := current_trace():
        with trace.lock:
            if not trace.closed:
                trace.error_code = code[:128]


def mark_disconnected() -> None:
    if trace := current_trace():
        with trace.lock:
            if not trace.closed:
                trace.disconnected = True


def mark_saved(message_id: int) -> None:
    if trace := current_trace():
        with trace.lock:
            if not trace.closed:
                trace.message_id, trace.saved = message_id, True


def mark_first_delta() -> None:
    if trace := current_trace():
        with trace.lock:
            if not trace.first_delta and not trace.closed:
                trace.first_delta = True
                trace.emit("answer.first_delta", "event", trace.root_id, None,
                           attributes={"elapsed_ms": int((time.monotonic() - trace.started) * 1000)})


def mark_business_outcome(outcome: str) -> None:
    """业务终态与传输状态分开记录；不因 HTTP 200 推断业务成功。"""
    if trace := current_trace():
        with trace.lock:
            if not trace.closed:
                trace.attributes.update(_safe({"business_outcome": outcome}))


def traced_stream(iterator, **attributes):
    """每次推进时绑定阶段；不把 ContextVar 令牌跨 yield 或线程保存。"""
    span = stage("answer.execute", **attributes)
    sentinel = object()
    try:
        while True:
            with span.activate():
                item = next(iterator, sentinel)
            if item is sentinel:
                span.finish("error", error_code="incomplete_stream")
                return
            if item["type"] == "done":
                span.finish(source_count=len(item.get("sources", [])),
                            tool_source_count=len(item.get("tool_sources", [])))
            yield item
            if item["type"] == "done":
                return
    except BaseException as exc:
        span.finish(exception_status(exc), error_code=getattr(exc, "code", type(exc).__name__))
        raise
    finally:
        span.finish("cancelled", reason="stream_closed")
