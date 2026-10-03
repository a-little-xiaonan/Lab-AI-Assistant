"""回答依据路由：分类校验、分支边界、混合回答和聊天协议。"""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from app.config import settings
from app.core import intent_router
from app.llm.errors import LLMError


def _plan(route, parts=None, question=""):
    return intent_router.IntentPlan(
        route=route,
        parts=tuple(intent_router.IntentPart(text, kind) for text, kind in (parts or [])),
        clarifying_question=question,
    )


def _frames(response):
    frames = []
    current = None
    for line in response.iter_lines():
        if line.startswith("event: "):
            current = {"event": line[7:]}
        elif line.startswith("data: ") and current is not None:
            current["data"] = json.loads(line[6:])
            frames.append(current)
            current = None
    return frames


def test_parse_plan_rejects_unsafe_or_malformed_mixed_output():
    for raw in (
        '{"route":"mixed","parts":[{"question":"A","route":"general"}]}',
        '{"route":"mixed","parts":[{"question":"A","route":"general"},{"question":"B","route":"general"}]}',
        '{"route":"mixed","parts":[{"question":"A","route":"general"},{"question":"B","route":"admin"}]}',
        '{"route":"clarify","clarifying_question":""}',
        'not json',
    ):
        with pytest.raises(LLMError) as exc:
            intent_router._parse_plan(raw)
        assert exc.value.code == "intent_route_failed"


def test_classifier_uses_bounded_llm_and_validates_output(monkeypatch):
    classifier = Mock(return_value='{"route":"general","parts":[],"clarifying_question":""}')
    monkeypatch.setattr(intent_router.qwen, "chat_completion", classifier)
    plan = intent_router._classify("大一该学什么？", "user: 我刚上大学")
    assert plan.route == "general"
    messages = classifier.call_args.args[0]
    assert "最近对话" in messages[1]["content"]
    assert "大一该学什么？" in messages[1]["content"]


def test_general_answer_skips_knowledge_and_has_no_sources(monkeypatch):
    monkeypatch.setattr(intent_router, "_classify", lambda *a, **k: _plan("general"))
    knowledge = Mock(side_effect=AssertionError("knowledge should not be called"))
    monkeypatch.setattr(intent_router.rag_pipeline, "answer", knowledge)
    generator = Mock(return_value="先打好基础，再做项目。")
    monkeypatch.setattr(intent_router.qwen, "chat_completion", generator)
    result = intent_router.answer("大一该学什么？", ["kb_default"])
    assert result == {"answer": "先打好基础，再做项目。", "sources": []}
    assert "不要编造实验室" in generator.call_args.args[0][0]["content"]
    knowledge.assert_not_called()


def test_knowledge_uses_authorized_scope_and_agent_switch(monkeypatch):
    monkeypatch.setattr(intent_router, "_classify", lambda *a, **k: _plan("knowledge"))
    rag = Mock(return_value={"answer": "资料回答", "sources": []})
    agent = Mock(return_value={"answer": "工具回答", "sources": []})
    monkeypatch.setattr(intent_router.rag_pipeline, "answer", rag)
    monkeypatch.setattr(intent_router.agent, "answer", agent)
    monkeypatch.setattr(settings, "agent_enabled", False)
    assert intent_router.answer("实验室怎么报名？", ["kb_readable"])["answer"] == "资料回答"
    assert rag.call_args.args[1] == ["kb_readable"]
    monkeypatch.setattr(settings, "agent_enabled", True)
    assert intent_router.answer("实验室怎么报名？", ["kb_readable"])["answer"] == "工具回答"
    assert agent.call_args.args[1] == ["kb_readable"]


def test_mixed_answer_keeps_missing_knowledge_separate(monkeypatch):
    monkeypatch.setattr(intent_router, "_classify", lambda *a, **k: _plan("mixed", [
        ("大一怎么学 Python？", "general"), ("实验室培训几点开始？", "knowledge")]))
    monkeypatch.setattr(settings, "agent_enabled", False)
    monkeypatch.setattr(intent_router.qwen, "chat_completion", Mock(return_value="每天练习并复盘。"))
    source = {"source_file": "安排.md", "page": 1, "snippet": "培训时间"}
    monkeypatch.setattr(intent_router.rag_pipeline, "answer", Mock(return_value={
        "answer": "目前没有足够资料确认培训时间。", "sources": [source]}))
    result = intent_router.answer("大一怎么学 Python？实验室培训几点开始？", ["kb_default"])
    assert "每天练习并复盘" in result["answer"]
    assert "目前没有足够资料确认培训时间" in result["answer"]
    assert result["sources"] == [source]


def test_tool_disabled_and_clarification_do_not_claim_knowledge(monkeypatch):
    monkeypatch.setattr(settings, "agent_enabled", False)
    monkeypatch.setattr(intent_router, "_classify", lambda *a, **k: _plan("tool"))
    assert "无法核实" in intent_router.answer("今天北京天气如何？", ["kb_default"])["answer"]
    monkeypatch.setattr(intent_router, "_classify", lambda *a, **k: _plan("clarify", question="请问你指的是哪场活动？"))
    assert intent_router.answer("那个什么时候开始？", ["kb_default"])["answer"] == "请问你指的是哪场活动？"


def test_tool_enabled_delegates_to_agent(monkeypatch):
    monkeypatch.setattr(settings, "agent_enabled", True)
    monkeypatch.setattr(intent_router, "_classify", lambda *a, **k: _plan("tool"))
    delegated = Mock(return_value={"answer": "日期结果", "sources": [], "tool_sources": [{"type": "datetime"}]})
    monkeypatch.setattr(intent_router.agent, "answer", delegated)
    result = intent_router.answer("今天星期几？", ["kb_readable"])
    assert result["tool_sources"] == [{"type": "datetime"}]
    assert delegated.call_args.args[1] == ["kb_readable"]


def test_mixed_general_and_tool_preserves_tool_provenance(monkeypatch):
    monkeypatch.setattr(settings, "agent_enabled", True)
    monkeypatch.setattr(intent_router, "_classify", lambda *a, **k: _plan("mixed", [
        ("大一应该怎样安排学习？", "general"), ("今天星期几？", "tool")]))
    monkeypatch.setattr(intent_router.qwen, "chat_completion", Mock(return_value="先建立稳定的学习节奏。"))
    tool = Mock(return_value={"answer": "星期三", "sources": [],
                              "tool_sources": [{"type": "datetime", "provider": "server_clock"}]})
    monkeypatch.setattr(intent_router.agent, "answer", tool)
    result = intent_router.answer("大一应该怎样安排学习？今天星期几？", ["kb_readable"])
    assert "先建立稳定的学习节奏" in result["answer"]
    assert "星期三" in result["answer"]
    assert result["tool_sources"] == [{"type": "datetime", "provider": "server_clock"}]
    assert tool.call_args.args[1] == ["kb_readable"]


def test_mixed_stream_discards_incomplete_part_and_keeps_other_part(monkeypatch):
    monkeypatch.setattr(settings, "agent_enabled", False)
    monkeypatch.setattr(intent_router, "_classify", lambda *a, **k: _plan("mixed", [
        ("大一怎么学 Python？", "general"), ("实验室培训几点开始？", "knowledge")]))

    def interrupted(*args, **kwargs):
        yield "不完整的建议"
        raise LLMError("llm_stream_interrupted", "生成中断")

    def knowledge(*args, **kwargs):
        yield {"type": "delta", "text": "资料不足，无法确认培训时间。"}
        yield {"type": "done", "full_text": "资料不足，无法确认培训时间。", "sources": []}

    monkeypatch.setattr(intent_router.qwen, "chat_completion_stream", interrupted)
    monkeypatch.setattr(intent_router.rag_pipeline, "answer_stream", knowledge)
    items = list(intent_router.answer_stream("大一怎么学 Python？实验室培训几点开始？", ["kb_default"]))
    shown = "".join(item["text"] for item in items if item["type"] == "delta")
    assert "不完整的建议" not in shown
    assert intent_router.PART_FAILURE in shown
    assert "资料不足，无法确认培训时间" in shown
    assert items[-1]["full_text"] == shown


def test_api_router_general_stream_and_nonstream(client, monkeypatch):
    from app.api.conversation import chat
    monkeypatch.setattr(settings, "intent_router_enabled", True)
    monkeypatch.setattr(settings, "agent_enabled", False)
    monkeypatch.setattr(chat, "_schedule_auto_name", lambda *a: None)
    monkeypatch.setattr(intent_router, "_classify", lambda *a, **k: _plan("general"))
    monkeypatch.setattr(intent_router.qwen, "chat_completion", Mock(return_value="先学会持续学习。"))
    stream_model = Mock(return_value=iter(["先学会", "持续学习。"]))
    monkeypatch.setattr(intent_router.qwen, "chat_completion_stream", stream_model)
    response = client.post("/api/chat", json={"message": "大一该学什么？"})
    assert response.status_code == 200
    assert response.json() == {"answer": "先学会持续学习。", "sources": []}
    with client.stream("POST", "/api/chat", json={"message": "大一该学什么？", "stream": True}) as stream:
        frames = _frames(stream)
    assert [frame["event"] for frame in frames] == ["meta", "delta", "delta", "done"]
    assert frames[-1]["data"]["full_text"] == "先学会持续学习。"
    assert frames[-1]["data"]["sources"] == []


def test_api_route_failure_is_error_not_no_context(client, monkeypatch):
    from app.api.conversation import chat
    monkeypatch.setattr(settings, "intent_router_enabled", True)
    monkeypatch.setattr(chat, "_schedule_auto_name", lambda *a: None)
    def fail(*args, **kwargs):
        raise LLMError("intent_route_failed", "问题分类暂时失败，请稍后重试。")
    monkeypatch.setattr(intent_router, "_classify", fail)
    response = client.post("/api/chat", json={"message": "大一该学什么？"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "intent_route_failed"
    with client.stream("POST", "/api/chat", json={"message": "大一该学什么？", "stream": True}) as stream:
        frames = _frames(stream)
    assert frames[-1]["event"] == "error"
    assert frames[-1]["data"]["code"] == "intent_route_failed"
