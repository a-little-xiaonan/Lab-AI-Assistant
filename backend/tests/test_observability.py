"""追踪隔离、流式终态、隐私边界、导出故障及查询工具。"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path

import pytest

from app.config import settings
from app.core import intent_router
from app.core.agent.models import ToolContext
from app.core.agent.runtime import ExecutionBudget, run_bounded
from app.llm.errors import LLMError
from app.observability import exporter, tracing
from app.observability.exporter import JsonlExporter
from app.observability.middleware import ChatTraceMiddleware
from app.tools.core.platform import ToolPlatform


class Collector:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def emit(self, event):
        with self.lock:
            self.events.append(event)

    def stats(self):
        return {"dropped_events": 0, "write_failures": 0}


@pytest.fixture
def collector(monkeypatch):
    sink = Collector()
    monkeypatch.setattr(settings, "observability_enabled", True)
    monkeypatch.setattr(exporter, "_exporter", sink)
    monkeypatch.setattr("app.main.start_exporter", lambda: None)
    monkeypatch.setattr("app.main.stop_exporter", lambda: None)
    monkeypatch.setattr("app.api.conversation.chat._schedule_auto_name", lambda *a: None)
    return sink


@pytest.fixture
def disk_exporter(monkeypatch, tmp_path):
    """在应用启动前启用真实导出线程，但只写测试临时目录。"""
    monkeypatch.setattr(settings, "observability_enabled", True)
    monkeypatch.setattr(exporter, "ROOT", tmp_path)
    monkeypatch.setattr(exporter, "_exporter", None)
    monkeypatch.setattr("app.api.conversation.chat._schedule_auto_name", lambda *a: None)
    return tmp_path / "data/logs/traces"


def test_application_lifespan_exports_jsonl(disk_exporter, client, monkeypatch):
    monkeypatch.setattr(intent_router, "answer", lambda *a, **k: {"answer": "正常回答", "sources": []})
    response = client.post("/api/chat", json={"message": "私密问题不应进入文件"})
    assert response.status_code == 200
    writer = exporter.get_exporter()
    assert writer is not None and writer.close()
    files = list(disk_exporter.glob("chat-trace-*.jsonl"))
    assert files
    text = "".join(path.read_text() for path in files)
    assert "私密问题" not in text
    events = [json.loads(line) for line in text.splitlines()]
    root = [e for e in events if e["stage"] == "chat.request" and e["event"] == "end"]
    assert len(root) == 1 and root[0]["status"] == "ok"
    assert root[0]["message_id"] and root[0]["session_id"]


def test_trace_metadata_allowlist_and_late_results():
    sink = Collector()
    trace = tracing.Trace(sink, "request-test")
    with tracing.bind_trace(trace):
        with tracing.stage("intent.classify", query="不能记录的问题正文", prompt="私密提示词", query_chars=10):
            pass
        pending = tracing.stage("answer.execute", route="general")
        trace.finish("cancelled")
        tracing.mark_saved(999)
        with tracing.stage("late.worker"):
            pass
        pending.finish()
        trace.finish("ok")
    assert "不能记录" not in json.dumps(sink.events, ensure_ascii=False)
    assert "私密提示词" not in json.dumps(sink.events, ensure_ascii=False)
    ends = [item for item in sink.events if item["stage"] == "chat.request" and item["event"] == "end"]
    assert len(ends) == 1 and ends[0]["status"] == "cancelled"
    assert not trace.saved
    assert all(item["late_result"] for item in sink.events if item["stage"] == "late.worker")


def test_bounded_threads_keep_request_and_span_context():
    from app.middleware.request_context import _REQUEST_ID, current_request_id
    sink = Collector()

    def request(index):
        token = _REQUEST_ID.set(f"request-{index}")
        try:
            trace = tracing.Trace(sink, current_request_id())
            with tracing.bind_trace(trace), tracing.stage("outer") as outer:
                def worker():
                    with tracing.stage("inner"):
                        return current_request_id(), tracing.current_trace().id
                observed = run_bounded(ExecutionBudget(time.monotonic() + 3), worker)
                assert observed == (f"request-{index}", trace.id)
                inner = [item for item in sink.events if item["trace_id"] == trace.id and item["stage"] == "inner"]
                assert all(item["parent_span_id"] == outer.id for item in inner)
            trace.finish("ok")
        finally:
            _REQUEST_ID.reset(token)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(request, range(3)))
    assert len({item["trace_id"] for item in sink.events}) == 3


def test_stream_span_can_resume_in_different_threads():
    sink = Collector()
    trace = tracing.Trace(sink, "request-stream")
    with tracing.bind_trace(trace):
        iterator = tracing.traced_stream(iter([
            {"type": "delta", "text": "正文"},
            {"type": "done", "full_text": "正文", "sources": []},
        ]), route="general")
        with ThreadPoolExecutor(max_workers=1) as first, ThreadPoolExecutor(max_workers=1) as second:
            assert first.submit(copy_context().run, next, iterator).result()["type"] == "delta"
            assert second.submit(copy_context().run, next, iterator).result()["type"] == "done"
        iterator.close()
        trace.finish("ok")
    ends = [item for item in sink.events if item["stage"] == "answer.execute" and item["event"] == "end"]
    assert len(ends) == 1 and ends[0]["status"] == "ok"


@pytest.mark.parametrize("streaming", [False, True])
def test_api_trace_is_private_and_bound_to_saved_message(client, monkeypatch, collector, streaming):
    calls = []

    def model(messages):
        if "只做路由" in messages[0]["content"]:
            from app.middleware.request_context import current_request_id
            calls.append((current_request_id(), tracing.current_trace().id))
            return '{"route":"general","parts":[],"clarifying_question":""}'
        return "正常回答"

    monkeypatch.setattr(intent_router.qwen, "chat_completion", model)
    monkeypatch.setattr(intent_router.qwen, "chat_completion_stream", lambda *a: iter(["正常", "回答"]))
    response = client.post("/api/chat", json={"message": "不应写入日志的私密问题", "stream": streaming},
                           headers={"X-Request-ID": "request-api-123"})
    assert response.status_code == 200
    assert "正常" in response.text
    assert "trace_id" not in response.text and "intent.classify" not in response.text
    assert "_evidence_refs" not in response.text and "_provenance" not in response.text
    events = collector.events
    root = [item for item in events if item["stage"] == "chat.request" and item["event"] == "end"]
    assert len(root) == 1 and root[0]["status"] == "ok"
    assert root[0]["session_id"] and root[0]["message_id"]
    assert root[0]["attributes"]["assistant_saved"] is True
    assert calls == [("request-api-123", root[0]["trace_id"])]
    assert "不应写入日志" not in json.dumps(events, ensure_ascii=False)
    assert {"history.snapshot", "intent.classify", "answer.execute", "message.persist"} <= {item["stage"] for item in events}


def test_sse_error_is_not_successful_http_200(client, monkeypatch, collector):
    monkeypatch.setattr(intent_router, "_classify", lambda *a: intent_router.IntentPlan("general"))

    def stream(*args):
        yield "部分正文"
        raise LLMError("llm_stream_interrupted", "中断")

    monkeypatch.setattr(intent_router.qwen, "chat_completion_stream", stream)
    response = client.post("/api/chat", json={"message": "问题", "stream": True})
    assert response.status_code == 200 and "event: error" in response.text
    root = [e for e in collector.events if e["stage"] == "chat.request" and e["event"] == "end"][0]
    assert root["status"] == "error" and root["attributes"]["error_code"] == "llm_stream_interrupted"
    assert root["attributes"]["assistant_saved"] is False


@pytest.mark.parametrize("streaming", [False, True])
def test_failed_message_commit_cannot_be_traced_as_saved(client, monkeypatch, collector, streaming):
    from sqlalchemy.orm import Session
    from app.models.database import AnswerProvenance

    original = Session.commit

    def commit(session):
        if any(isinstance(row, AnswerProvenance) for row in session.new):
            raise RuntimeError("模拟助手消息保存失败")
        return original(session)

    monkeypatch.setattr(Session, "commit", commit)
    monkeypatch.setattr(intent_router, "answer", lambda *a, **k: {"answer": "正文", "sources": []})
    monkeypatch.setattr(intent_router, "answer_stream", lambda *a, **k: iter([
        {"type": "delta", "text": "正文"}, {"type": "done", "full_text": "正文", "sources": []},
    ]))
    if streaming:
        response = client.post("/api/chat", json={"message": "问题", "stream": True})
        assert "event: error" in response.text and "event: done" not in response.text
    else:
        with pytest.raises(RuntimeError, match="模拟助手消息保存失败"):
            client.post("/api/chat", json={"message": "问题"})
    root = [e for e in collector.events if e["stage"] == "chat.request" and e["event"] == "end"][0]
    assert root["status"] == "error" and root["attributes"]["assistant_saved"] is False
    failed = [e for e in collector.events if e["stage"] == "message.persist" and e["status"] == "error"]
    assert len(failed) == 1


def test_agent_context_uses_same_http_and_trace_ids(client, monkeypatch, collector):
    from app.core.agent import orchestrator
    from app.tools import registry

    monkeypatch.setattr(settings, "agent_enabled", True)
    monkeypatch.setattr(intent_router, "_classify", lambda *a: intent_router.IntentPlan("tool"))
    decisions = iter([
        {"tool_calls": [{"id": "call-clock", "type": "function", "function": {
            "name": "get_current_datetime", "arguments": "{}"}}]},
        {"content": "完成"},
    ])
    monkeypatch.setattr(orchestrator.qwen, "tool_completion", lambda *a, **k: next(decisions))
    monkeypatch.setattr(orchestrator.qwen, "chat_completion", lambda *a, **k: "日期结果")
    execute, contexts = registry.execute, []

    def record_context(call, ctx):
        contexts.append(ctx)
        return execute(call, ctx)

    monkeypatch.setattr(registry, "execute", record_context)
    response = client.post("/api/chat", json={"message": "今天星期几？"}, headers={"X-Request-ID": "request-agent-123"})
    assert response.status_code == 200 and response.json()["answer"] == "日期结果"
    assert len(contexts) == 1
    assert contexts[0].request_id == "request-agent-123"
    assert contexts[0].trace_id == collector.events[0]["trace_id"]
    assert any(item["stage"] == "tool.execute" for item in collector.events)
    assert all(item["request_id"] == "request-agent-123" for item in collector.events)


@pytest.mark.parametrize("saved", [False, True])
def test_middleware_cancellation_preserves_commit_state(monkeypatch, collector, saved):
    async def app(scope, receive, send):
        if saved:
            tracing.mark_saved(123)
        raise asyncio.CancelledError

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await ChatTraceMiddleware(app)({"type": "http", "method": "POST", "path": "/api/chat"}, None, None)

    asyncio.run(run())
    root = collector.events[-1]
    assert root["status"] == ("ok" if saved else "cancelled")
    assert root["attributes"]["transport_complete"] is False
    assert root["attributes"]["assistant_saved"] is saved


def test_asgi_disconnect_ends_unfinished_stage(collector):
    async def app(scope, receive, send):
        tracing.stage("pending.generation")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        await receive()

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    asyncio.run(ChatTraceMiddleware(app)({"type": "http", "method": "POST", "path": "/api/chat"}, receive, send))
    assert collector.events[-1]["status"] == "cancelled"
    closed = [item for item in collector.events if item["stage"] == "pending.generation" and item["event"] == "end"]
    assert len(closed) == 1 and closed[0]["status"] == "cancelled"


def test_tool_early_return_is_observed():
    sink = Collector()
    trace = tracing.Trace(sink, "request-tool")
    with tracing.bind_trace(trace):
        ctx = ToolContext((), ExecutionBudget(time.monotonic() + 2), "Asia/Shanghai")
        result = ToolPlatform([]).execute({"id": "call-1", "function": {"name": "missing", "arguments": "{}"}}, ctx)
        trace.finish("ok")
    assert result.status == "unsupported"
    end = [e for e in sink.events if e["stage"] == "tool.execute" and e["event"] == "end"][0]
    assert end["attributes"]["outcome"] == "unsupported"
    assert end["attributes"]["error_code"] == "unknown_tool"


def test_exporter_rotation_retention_and_permissions(tmp_path):
    old = tmp_path / "chat-trace-old.jsonl"
    old.write_text("old")
    os.utime(old, (time.time() - 10 * 86400,) * 2)
    writer = JsonlExporter(tmp_path, queue_size=100, max_bytes=150, backup_count=2, retention_days=7)
    for index in range(12):
        writer.emit({"index": index, "payload": "x" * 50})
    assert writer.close()
    files = list(tmp_path.glob("chat-trace-*.jsonl*"))
    assert not old.exists()
    assert 1 <= len(files) <= 3
    for path in files:
        assert path.stat().st_size <= 150
        assert path.stat().st_mode & 0o077 == 0
        for line in path.read_text().splitlines():
            json.loads(line)


def test_exporter_queue_overflow_and_write_failure(tmp_path):
    started, release = threading.Event(), threading.Event()

    class SlowExporter(JsonlExporter):
        def _write(self, event):
            started.set()
            release.wait(2)
            raise OSError("模拟写入失败")

    writer = SlowExporter(tmp_path, queue_size=1, max_bytes=1024, backup_count=1, retention_days=1)
    writer.emit({"one": 1})
    assert started.wait(1)
    writer.emit({"two": 2})
    writer.emit({"three": 3})
    release.set()
    assert writer.close()
    assert writer.stats() == {"dropped_events": 1, "write_failures": 2}


def test_disabled_observation_keeps_response(client, monkeypatch, collector):
    monkeypatch.setattr(settings, "observability_enabled", False)
    monkeypatch.setattr(intent_router, "answer", lambda *a, **k: {"answer": "原有回答", "sources": []})
    assert client.post("/api/chat", json={"message": "问题"}).json() == {"answer": "原有回答", "sources": []}
    assert collector.events == []


def test_broken_exporter_does_not_break_answer(client, monkeypatch, collector):
    def fail(event):
        raise OSError("模拟写入错误")
    monkeypatch.setattr(collector, "emit", fail)
    monkeypatch.setattr(intent_router, "answer", lambda *a, **k: {"answer": "原有回答", "sources": []})
    response = client.post("/api/chat", json={"message": "问题"})
    assert response.status_code == 200 and response.json()["answer"] == "原有回答"


def test_trace_query_includes_start_before_session_binding(tmp_path):
    sink = Collector()
    trace = tracing.Trace(sink, "request-cli")
    with tracing.bind_trace(trace):
        tracing.bind_session("session-cli")
        with tracing.stage("intent.selected", route="general"):
            pass
        tracing.mark_saved(789)
        trace.finish("ok")
    path = tmp_path / "chat-trace-test.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in sink.events) + "\n")
    script = Path(__file__).resolve().parents[1] / "scripts/diagnostics/inspect_chat_trace.py"
    result = subprocess.run([sys.executable, str(script), "--directory", str(tmp_path), "--message-id", "789"],
                            capture_output=True, text=True, check=True)
    assert trace.id in result.stdout and "intent.selected" in result.stdout
    assert "记录可能不完整" not in result.stdout
