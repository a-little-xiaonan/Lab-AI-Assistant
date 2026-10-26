"""请求内阶段记录与线程安全关联；只接受明确允许的元数据。"""
from __future__ import annotations

import asyncio
import logging
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
_CURRENT: ContextVar[Trace | None] = ContextVar("chat_trace", default=None)
_PARENT: ContextVar[tuple[str, str] | None] = ContextVar("chat_span", default=None)
_FIELDS = frozenset({
    "stream", "http_status", "transport_complete", "disconnected", "assistant_saved",
    "route", "parts_count", "handler", "role", "outcome", "error_code", "reason",
    "query_chars", "history_chars", "history_before_chars", "history_messages",
    "history_dropped_messages", "history_summary_present", "readable_kb_count",
    "source_count", "tool_source_count", "tool", "call_id", "cache_hit",
    "dropped_events", "write_failures", "elapsed_ms", "part_id",
    "history_revision", "history_turns", "history_dropped_turns", "turn_status",
    "lease_acquired", "answer_kind", "basis_status",
})


def _safe(values: dict) -> dict:
    return {key: (value[:128] if isinstance(value, str) else value)
            for key, value in values.items() if key in _FIELDS
            and (value is None or isinstance(value, (str, bool, int)))}


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
                "schema_version": "chat-trace-v1", "pipeline_version": "intent-history-p2",
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
            except Exception:
                stats = {}
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
