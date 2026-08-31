"""聊天 API 层测试：SSE 帧序 / 错误帧 / 断连不落库 / 流结束落库 / 非流式回归。

LLM 与检索全部 patch 掉（不依赖 API key 与 ChromaDB），只测 API 层行为。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from sqlalchemy import select

from app.api.chat import _sse_stream
from app.llm.errors import LLMError
from app.models.database import ChatSession, Message
from app.models.schemas import ChatRequest


def _parse_sse_frames(lines: list[str]) -> list[dict]:
    """把 SSE 逐行输出解析为 [{event, data}, ...]。"""
    frames = []
    current = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("event: "):
            current = {"event": line[len("event: "):], "data": None}
        elif line.startswith("data: ") and current is not None:
            current["data"] = json.loads(line[len("data: "):])
            frames.append(current)
            current = None
    return frames


def _fake_stream(deltas, full_text=None, sources=None):
    def gen():
        for d in deltas:
            yield {"type": "delta", "text": d}
        yield {
            "type": "done",
            "full_text": full_text or "".join(deltas),
            "sources": sources or [],
        }

    return gen()


def test_chat_stream_full_flow(client):
    """stream=true：meta → delta* → done 帧序正确，流结束后 user+assistant 完整落库。"""
    deltas = ["你", "好，", "有什么可以帮您？"]
    sources = [{"source_file": "手册.pdf", "page": 3, "snippet": "内容片段"}]
    with patch(
        "app.api.chat.answer_stream", side_effect=lambda *a, **k: _fake_stream(deltas, sources=sources)
    ):
        with client.stream(
            "POST", "/api/chat", json={"message": "你好", "stream": True}
        ) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            assert r.headers.get("cache-control") == "no-cache"
            frames = _parse_sse_frames(list(r.iter_lines()))

    events = [f["event"] for f in frames]
    assert events == ["meta", "delta", "delta", "delta", "done"]
    session_id = frames[0]["data"]["session_id"]
    assert session_id.startswith("sess_")
    assert [f["data"]["text"] for f in frames if f["event"] == "delta"] == deltas
    done = frames[-1]["data"]
    assert done["full_text"] == "".join(deltas)
    assert done["sources"] == sources

    # 落库验收：流结束后消息完整
    resp = client.get(f"/api/chat/sessions/{session_id}")
    msgs = resp.json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"] == "".join(deltas)


def test_chat_stream_error_frame(client):
    """生成中途失败 → error 帧替代 done，HTTP 仍 200，assistant 不落库。"""
    def fake_stream():
        yield {"type": "delta", "text": "部分内容"}
        raise LLMError("llm_stream_interrupted", "流式输出中断")

    with patch("app.api.chat.answer_stream", side_effect=lambda *a, **k: fake_stream()):
        with client.stream(
            "POST", "/api/chat", json={"message": "你好", "stream": True}
        ) as r:
            assert r.status_code == 200
            frames = _parse_sse_frames(list(r.iter_lines()))

    events = [f["event"] for f in frames]
    assert events[-1] == "error"
    assert "done" not in events
    assert frames[-1]["data"] == {"code": "llm_stream_interrupted", "message": "流式输出中断"}

    session_id = frames[0]["data"]["session_id"]
    msgs = client.get(f"/api/chat/sessions/{session_id}").json()["messages"]
    assert [m["role"] for m in msgs] == ["user"]  # 只有 user，无半条 assistant


def test_chat_stream_disconnect_keeps_no_partial_message(db_session):
    """客户端断开（is_disconnected 第二次返回 True）→ 生成器提前结束、不写 assistant。"""
    class FakeRequest:
        def __init__(self):
            self.checks = 0

        async def is_disconnected(self):
            self.checks += 1
            return self.checks >= 2  # 首个 delta 后检查时断开

    def fake_stream():
        yield {"type": "delta", "text": "部分"}
        yield {"type": "delta", "text": "内容"}
        yield {"type": "done", "full_text": "部分内容", "sources": []}

    session = ChatSession(id="sess_disconnect_test", knowledge_base_id="kb_default")
    db_session.add(session)
    db_session.commit()

    req = ChatRequest(message="你好", stream=True)
    with patch("app.api.chat.answer_stream", side_effect=lambda *a, **k: fake_stream()):
        frames = asyncio.run(_collect_frames(_sse_stream(req, FakeRequest(), db_session, session)))

    assert frames[0].startswith("event: meta")
    assert not any("event: done" in f for f in frames)  # 未到达 done
    msgs = db_session.scalars(select(Message)).all()
    assert [m.role for m in msgs] == ["user"]


async def _collect_frames(gen):
    return [f async for f in gen]


def test_chat_non_stream_regression(client):
    """stream=false：响应结构与 Phase 1 一致，user+assistant 成对落库，未知 session 自动创建。"""
    with patch("app.api.chat.answer", return_value={"answer": "你好！", "sources": []}):
        r = client.post("/api/chat", json={"message": "你好"})
    assert r.status_code == 200
    assert r.json() == {"answer": "你好！", "sources": []}

    sessions = client.get("/api/chat/sessions").json()
    assert len(sessions) == 1
    detail = client.get(f"/api/chat/sessions/{sessions[0]['id']}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]


def test_chat_empty_message_rejected(client):
    r = client.post("/api/chat", json={"message": "   "})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "empty_message"
