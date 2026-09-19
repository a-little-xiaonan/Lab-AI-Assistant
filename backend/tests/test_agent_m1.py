"""M1 离线验收：固定时钟、模拟模型和检索，不请求供应商。"""
import json
import time
from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.config import settings
from app.core import rag_pipeline
from app.core.agent import orchestrator
from app.core.agent.models import ToolContext
from app.core.agent.runtime import ExecutionBudget, run_bounded
from app.core.retrieval.retriever import RetrievedChunk
from app.llm import qwen
from app.llm.errors import LLMError
from app.tools import registry
from app.tools.local.datetime import DatetimeArgs, get_current_datetime


def context(kbs=()):
    return ToolContext(tuple(kbs), ExecutionBudget(time.monotonic() + 5), "Asia/Shanghai")


def call(name="get_current_datetime", arguments=None, cid="call_1"):
    return {"id": cid, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments or {}, ensure_ascii=False)}}


def route(*calls):
    return {"role": "assistant", "content": "未执行前不能展示的文本", "tool_calls": list(calls)}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(qwen.Generation, "call", Mock(side_effect=AssertionError("unexpected network")))
    monkeypatch.setattr(qwen.MultiModalConversation, "call", Mock(side_effect=AssertionError("unexpected network")))
    monkeypatch.setattr(settings, "agent_enabled", False)


@pytest.mark.parametrize("zone,date,weekday", [
    ("Asia/Shanghai", "2026-09-18", "星期五"),
    ("UTC", "2026-09-17", "星期四"),
    ("America/New_York", "2026-09-17", "星期四"),
])
def test_fixed_clock(zone, date, weekday):
    result = get_current_datetime(DatetimeArgs(timezone=zone), context(),
                                  lambda: datetime(2026, 9, 17, 17, 0, tzinfo=timezone.utc))
    assert result.status == "ok"
    assert result.data["date"] == date
    assert result.data["weekday"] == weekday
    assert result.data["timezone"] == zone


@pytest.mark.parametrize("zone", ["Mars/Olympus", "", "../etc/passwd", "/etc/passwd"])
def test_invalid_timezone(zone):
    assert get_current_datetime(DatetimeArgs(timezone=zone), context()).error.code == "invalid_timezone"


def test_dst_clock():
    ctx = context()
    before = get_current_datetime(DatetimeArgs(timezone="America/New_York"), ctx,
                                 lambda: datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc))
    after = get_current_datetime(DatetimeArgs(timezone="America/New_York"), ctx,
                                lambda: datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc))
    assert "01:59:00-05:00" in before.data["datetime"]
    assert "03:00:00-04:00" in after.data["datetime"]


@pytest.mark.parametrize("args", [{"timezone": 123}, {"user_id": "admin"}, {"kb_ids": ["private"]}])
def test_args_forbid_privilege_injection(args):
    result = registry.execute(call(arguments=args), context())
    assert result.error.code == "invalid_arguments"


def test_unknown_and_malformed():
    assert registry.execute(call("exec"), context()).status == "unsupported"
    bad = call()
    bad["function"]["arguments"] = "not json"
    assert registry.execute(bad, context()).error.code == "invalid_arguments"


def test_call_budget_and_cache(monkeypatch):
    monkeypatch.setattr(settings, "agent_max_tool_calls", 2)
    ctx = context()
    first = registry.execute(call(), ctx)
    second = registry.execute(call(cid="call_2"), ctx)
    assert second.cache_hit and second.call_id == "call_2"
    assert first.evidence_refs == second.evidence_refs
    assert registry.execute(call(cid="call_3"), ctx).error.code == "call_budget_exhausted"
    assert len(ctx.evidence_ledger) == 1


def test_cancelled_does_not_execute():
    ctx = context()
    ctx.budget.cancelled.set()
    assert registry.execute(call(), ctx).error.code == "prepare_timeout"
    assert not ctx.evidence_ledger


def test_bounded_discards_late_result():
    release = Event()
    finished = Event()
    budget = ExecutionBudget(time.monotonic() + 0.03)

    def slow():
        release.wait(1)
        finished.set()
        return "late"

    try:
        with pytest.raises(TimeoutError):
            run_bounded(budget, slow)
        assert budget.cancelled.is_set()
    finally:
        release.set()
        assert finished.wait(1)


def test_sdk_contract(monkeypatch):
    monkeypatch.setattr(settings, "dashscope_api_key", "unit-test-only")
    monkeypatch.setattr(settings, "llm_model", "qwen-plus")
    monkeypatch.setattr(settings, "dashscope_base_url", "https://workspace.example/api/v1")
    sdk = Mock(return_value=SimpleNamespace(status_code=200, output={"choices": [{"message": route(call())}]}))
    monkeypatch.setattr(qwen.Generation, "call", sdk)
    result = qwen.tool_completion([{"role": "user", "content": "今天？"}], registry.schemas(), timeout=2)
    assert result["tool_calls"][0]["id"] == "call_1"
    assert sdk.call_args.kwargs["result_format"] == "message"
    assert sdk.call_args.kwargs["timeout"] == 2
    assert sdk.call_args.kwargs["base_address"] == "https://workspace.example/api/v1"


def test_chat_completion_uses_configured_base_address(monkeypatch):
    monkeypatch.setattr(settings, "dashscope_api_key", "unit-test-only")
    monkeypatch.setattr(settings, "llm_model", "qwen-plus")
    monkeypatch.setattr(settings, "dashscope_base_url", "https://workspace.example/api/v1")
    sdk = Mock(return_value=SimpleNamespace(status_code=200, output={"text": "ok"}))
    monkeypatch.setattr(qwen.Generation, "call", sdk)
    assert qwen.chat_completion([{"role": "user", "content": "你好"}]) == "ok"
    assert sdk.call_args.kwargs["base_address"] == "https://workspace.example/api/v1"


def test_multimodal_chat_converts_text_and_reads_choice(monkeypatch):
    monkeypatch.setattr(settings, "dashscope_api_key", "unit-test-only")
    monkeypatch.setattr(settings, "llm_model", "qwen3.8-flash")
    sdk = Mock(return_value=SimpleNamespace(status_code=200, output={"choices": [{
        "message": {"content": [{"text": "回答"}], "reasoning_content": "不应展示"}}]}))
    monkeypatch.setattr(qwen.MultiModalConversation, "call", sdk)
    assert qwen.chat_completion([{"role": "user", "content": "问题"}]) == "回答"
    assert sdk.call_args.kwargs["messages"] == [{"role": "user", "content": [{"text": "问题"}]}]
    assert sdk.call_args.kwargs["result_format"] == "message"


def test_multimodal_stream_reads_incremental_text(monkeypatch):
    monkeypatch.setattr(settings, "dashscope_api_key", "unit-test-only")
    monkeypatch.setattr(settings, "llm_model", "qwen3.8-flash")
    chunks = [SimpleNamespace(status_code=200, output={"choices": [{"message": {
        "content": [{"text": value}]}}]}) for value in ("你", "好")]
    sdk = Mock(return_value=iter(chunks))
    monkeypatch.setattr(qwen.MultiModalConversation, "call", sdk)
    assert list(qwen.chat_completion_stream([{"role": "user", "content": "你好"}])) == ["你", "好"]
    assert sdk.call_args.kwargs["stream"] is True
    assert sdk.call_args.kwargs["incremental_output"] is True


def test_multimodal_tool_call_preserves_tool_ids(monkeypatch):
    monkeypatch.setattr(settings, "dashscope_api_key", "unit-test-only")
    monkeypatch.setattr(settings, "llm_model", "qwen3.8-flash")
    sdk = Mock(return_value=SimpleNamespace(status_code=200, output={"choices": [{
        "message": {"content": [], "tool_calls": [call()]}}]}))
    monkeypatch.setattr(qwen.MultiModalConversation, "call", sdk)
    messages = [{"role": "user", "content": "今天？"},
                {"role": "tool", "tool_call_id": "old", "content": "结果"}]
    result = qwen.tool_completion(messages, registry.schemas(), timeout=2)
    assert result["tool_calls"][0]["id"] == "call_1"
    assert sdk.call_args.kwargs["messages"][1] == {"role": "tool", "tool_call_id": "old",
                                                  "content": [{"text": "结果"}]}


@pytest.mark.parametrize("message", [route(call(), call()), route({"id": "x"}), {"content": 123}])
def test_sdk_rejects_malformed(monkeypatch, message):
    monkeypatch.setattr(settings, "dashscope_api_key", "unit-test-only")
    monkeypatch.setattr(settings, "llm_model", "qwen-plus")
    monkeypatch.setattr(qwen.Generation, "call", Mock(return_value=SimpleNamespace(
        status_code=200, output={"choices": [{"message": message}]})))
    with pytest.raises(LLMError):
        qwen.tool_completion([], registry.schemas(), timeout=2)


def test_date_route_without_kb_and_tool_id_roundtrip(monkeypatch):
    decision = Mock(side_effect=[route(call()), {"role": "assistant", "content": "stop"}])
    monkeypatch.setattr(qwen, "tool_completion", decision)
    final = Mock(return_value="时钟查询成功。")
    monkeypatch.setattr(qwen, "chat_completion", final)
    retrieval = Mock(side_effect=AssertionError("date must not retrieve"))
    monkeypatch.setattr(rag_pipeline, "prepare_evidence", retrieval)
    result = orchestrator.answer("今天星期几？", [])
    assert result["tool_sources"][0]["type"] == "datetime"
    assert result["sources"] == []
    messages = decision.call_args.args[0]
    assert messages[-1]["role"] == "tool"
    assert messages[-1]["tool_call_id"] == "call_1"
    assert "未执行前不能展示" not in result["answer"]
    retrieval.assert_not_called()
    final.assert_called_once()


def test_route_failure_never_says_knowledge_missing(monkeypatch):
    monkeypatch.setattr(qwen, "tool_completion", Mock(side_effect=LLMError("failed", "private details")))
    result = orchestrator.answer("北京天气？", [])
    assert "工具选择" in result["answer"]
    assert "知识库" not in result["answer"]
    assert "private details" not in result["answer"]


def test_no_tool_fact_text_not_accepted(monkeypatch):
    monkeypatch.setattr(qwen, "tool_completion", Mock(return_value={"role": "assistant", "content": "北京晴天 30 度"}))
    assert "30" not in orchestrator.answer("北京天气？", [])["answer"]


def test_no_scope_does_not_retrieve(monkeypatch):
    retrieve = Mock()
    monkeypatch.setattr(rag_pipeline, "prepare_evidence", retrieve)
    result = registry.execute(call("search_knowledge", {"question": "报名"}), context())
    assert result.status == "no_data"
    assert result.data["reason"] == "no_readable_scope"
    retrieve.assert_not_called()


def test_retrieval_failure_distinct_from_empty(monkeypatch):
    monkeypatch.setattr(settings, "query_planning_enabled", False)
    monkeypatch.setattr(settings, "topic_retrieval_enabled", False)
    monkeypatch.setattr(rag_pipeline.retriever, "retrieve", Mock(side_effect=RuntimeError("db down")))
    result = registry.execute(call("search_knowledge", {"question": "报名"}), context(["allowed"]))
    assert result.status == "failed"
    assert "db down" not in result.model_dump_json()
    monkeypatch.setattr(rag_pipeline.retriever, "retrieve", Mock(return_value=[]))
    empty = registry.execute(call("search_knowledge", {"question": "报名"}), context(["allowed"]))
    assert empty.status == "no_data"


def test_publication_final_check(monkeypatch):
    chunk = RetrievedChunk("c", "private", .9, {"doc_id": "d"})
    monkeypatch.setattr(settings, "query_planning_enabled", False)
    monkeypatch.setattr(settings, "topic_retrieval_enabled", False)
    monkeypatch.setattr(rag_pipeline.retriever, "retrieve", Mock(return_value=[chunk]))
    check = Mock(return_value=[])
    monkeypatch.setattr(rag_pipeline.retriever, "filter_published", check)
    result = registry.execute(call("search_knowledge", {"question": "报名"}), context(["allowed"]))
    assert result.status == "no_data"
    assert "private" not in result.model_dump_json()
    assert check.call_args.args[0] == "allowed"


def test_mixed_date_and_failed_knowledge(monkeypatch):
    monkeypatch.setattr(qwen, "tool_completion", Mock(side_effect=[
        route(call(), call("search_knowledge", {"question": "报名"}, "call_2")),
        {"role": "assistant", "content": "done"},
    ]))
    monkeypatch.setattr(rag_pipeline, "prepare_evidence", Mock(side_effect=RuntimeError("db down")))
    final = Mock(return_value="日期已查到；知识查询暂时失败。")
    monkeypatch.setattr(qwen, "chat_completion", final)
    result = orchestrator.answer("今天日期和报名条件？", ["allowed"])
    payload = json.loads(final.call_args.args[0][1]["content"])
    assert [r["status"] for r in payload["results"]] == ["ok", "failed"]
    assert result["tool_sources"][0]["type"] == "datetime"


def test_round_limit(monkeypatch):
    monkeypatch.setattr(settings, "agent_max_tool_rounds", 2)
    decisions = Mock(side_effect=[route(call(cid="one")), route(call(cid="two"))])
    monkeypatch.setattr(qwen, "tool_completion", decisions)
    monkeypatch.setattr(qwen, "chat_completion", Mock(return_value="部分结果"))
    orchestrator.answer("今天？", [])
    assert decisions.call_count == 2


def test_stream_and_nonstream_equivalence(monkeypatch):
    monkeypatch.setattr(qwen, "tool_completion", Mock(side_effect=[route(call()), {"content": "done"}]))
    monkeypatch.setattr(qwen, "chat_completion_stream", Mock(return_value=iter(["时钟", "结果"])))
    items = list(orchestrator.answer_stream("今天？", []))
    assert items[0]["type"] == "status"
    assert items[-1]["type"] == "done"
    assert items[-1]["full_text"] == "".join(i["text"] for i in items if i["type"] == "delta")


def test_api_switch_and_scope(client, monkeypatch):
    from app.api.conversation import chat
    monkeypatch.setattr(settings, "agent_enabled", True)
    monkeypatch.setattr(chat, "_schedule_auto_name", lambda *a: None)
    agent = Mock(return_value={"answer": "日期", "sources": [], "tool_sources": []})
    monkeypatch.setattr(chat.agent, "answer", agent)
    legacy = Mock(side_effect=AssertionError("legacy called"))
    monkeypatch.setattr(chat, "answer", legacy)
    response = client.post("/api/chat", json={"message": "今天？"})
    assert response.status_code == 200
    assert "tool_sources" in response.json()
    assert agent.call_args.args[1] == ["kb_default"]
    legacy.assert_not_called()


def test_agent_turn_never_extracts_tool_answer(monkeypatch):
    from app.api.conversation import chat
    monkeypatch.setattr(settings, "agent_enabled", True)
    extraction = Mock()
    monkeypatch.setattr(chat, "_schedule_memory_extract", extraction)
    monkeypatch.setattr(chat, "_schedule_auto_name", lambda *a: None)
    monkeypatch.setattr(chat.memory_manager, "persist_summary", lambda *a: None)
    chat._remember_turn("session", "kb", "今天？", "2026-09-17", "user")
    extraction.assert_not_called()


def test_cancel_during_route_starts_no_tool(monkeypatch):
    cancelled = Event()

    def decide(*args, **kwargs):
        cancelled.set()
        return route(call())

    monkeypatch.setattr(qwen, "tool_completion", decide)
    execute = Mock()
    monkeypatch.setattr(registry, "execute", execute)
    items = list(orchestrator.answer_stream("今天？", [], cancellation=cancelled))
    assert [i["type"] for i in items] == ["status"]
    execute.assert_not_called()


def test_partial_scope_failure_keeps_success(monkeypatch):
    monkeypatch.setattr(settings, "query_planning_enabled", False)
    monkeypatch.setattr(settings, "topic_retrieval_enabled", False)
    chunk = RetrievedChunk("c", "报名需要面试。", .9, {"doc_id": "d"})

    def retrieve(kb, query):
        if kb == "broken":
            raise RuntimeError("offline")
        assert kb == "allowed"
        return [chunk]

    monkeypatch.setattr(rag_pipeline.retriever, "retrieve", retrieve)
    monkeypatch.setattr(rag_pipeline.retriever, "filter_published", lambda kb, chunks: chunks)
    result = registry.execute(call("search_knowledge", {"question": "报名"}), context(["allowed", "broken"]))
    assert result.status == "ok"
    assert result.data["partial_failure"]
    assert result.error.code == "knowledge_service_failed"
    assert result.data["chunks"][0]["knowledge_base_id"] == "allowed"


def test_context_cache_does_not_cross_requests():
    one, two = context(), context()
    registry.execute(call(), one)
    assert not two.cache and not two.evidence_ledger
    assert not registry.execute(call(), two).cache_hit


def test_api_agent_status_and_sources(client, monkeypatch):
    from app.api.conversation import chat
    monkeypatch.setattr(settings, "agent_enabled", True)
    monkeypatch.setattr(chat, "_schedule_auto_name", lambda *a: None)

    def stream(*args, **kwargs):
        yield {"type": "status", "message": "正在读取时钟"}
        yield {"type": "delta", "text": "日期结果"}
        yield {"type": "done", "full_text": "日期结果", "sources": [],
               "tool_sources": [{"type": "datetime", "provider": "server_clock"}]}

    monkeypatch.setattr(chat.agent, "answer_stream", stream)
    with client.stream("POST", "/api/chat", json={"message": "今天？", "stream": True}) as response:
        text = "\n".join(response.iter_lines())
    assert text.index("event: meta") < text.index("event: status") < text.index("event: delta") < text.index("event: done")
    assert '"tool_sources"' in text


def test_empty_stream_becomes_error(client, monkeypatch):
    from app.api.conversation import chat
    monkeypatch.setattr(chat, "answer_stream", lambda *a, **kw: iter([]))
    with client.stream("POST", "/api/chat", json={"message": "今天？", "stream": True}) as response:
        text = "\n".join(response.iter_lines())
    assert "incomplete_stream" in text
    assert "event: done" not in text
