"""qwen.chat_completion_stream 单测：增量/差分兜底/首块重试/中断/空响应。

patch 统一打在模型调用封装上，避免本地模型配置影响普通/多模态调用路径。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from unittest.mock import patch

from app.llm import qwen
from app.llm.errors import LLMError


def _chunk(text: str, status: int = 200, code: str | None = None, message: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status, code=code, message=message, output={"text": text}
    )


def _stream_resp(chunks):
    return (c for c in chunks)


def test_stream_incremental_mode_yields_deltas():
    """incremental 形态（text 即增量）：直接产出。"""
    fake = _stream_resp(
        [_chunk("1"), _chunk("  \n2"), _chunk("3"), _chunk("")]
    )
    with patch("app.llm.qwen._model_call", return_value=fake):
        out = list(qwen.chat_completion_stream([{"role": "user", "content": "x"}]))
    assert out == ["1", "  \n2", "3"]


def test_stream_merge_mode_diffs_accumulated_text():
    """merge 形态（text 累积全文）：差分兜底只产出增量。"""
    fake = _stream_resp([_chunk("1"), _chunk("1  \n2"), _chunk("1  \n2  \n3"), _chunk("")])
    with patch("app.llm.qwen._model_call", return_value=fake):
        out = list(qwen.chat_completion_stream([{"role": "user", "content": "x"}]))
    assert out == ["1", "  \n2", "  \n3"]


def test_stream_mid_chunk_error_raises_interrupted():
    """中途块非 200 → llm_stream_interrupted（不重试，防重复输出）。"""
    fake = _stream_resp([_chunk("好的"), _chunk("", status=500, message="boom")])
    with patch("app.llm.qwen._model_call", return_value=fake):
        with pytest.raises(LLMError) as ei:
            list(qwen.chat_completion_stream([{"role": "user", "content": "x"}]))
    assert ei.value.code == "llm_stream_interrupted"


def test_stream_retries_retryable_first_chunk():
    """首块 429 → 重试后成功（第二次调用返回正常流）。"""
    calls = {"n": 0}

    def _side_effect(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _stream_resp([_chunk("", status=429)])
        return _stream_resp([_chunk("重试成功"), _chunk("")])

    with patch("app.llm.qwen._model_call", side_effect=_side_effect):
        out = list(qwen.chat_completion_stream([{"role": "user", "content": "x"}]))
    assert calls["n"] == 2
    assert out == ["重试成功"]


def test_stream_retry_exhausted_raises():
    """首块持续 429 → 重试耗尽后抛 LLMError（llm_status_429）。

    注意用 side_effect：每次调用返回新生成器（模拟真实 SDK；return_value 会复用已耗尽的流）。
    """

    def _always_429(**kwargs):
        return _stream_resp([_chunk("", status=429)])

    with patch("app.llm.qwen._model_call", side_effect=_always_429):
        with pytest.raises(LLMError) as ei:
            list(qwen.chat_completion_stream([{"role": "user", "content": "x"}]))
    assert ei.value.code == "llm_status_429"


def test_stream_empty_output_raises():
    """全程空块（只有流结束标记）→ llm_empty_response（与非流式口径一致）。"""
    fake = _stream_resp([_chunk(""), _chunk("")])
    with patch("app.llm.qwen._model_call", return_value=fake):
        with pytest.raises(LLMError) as ei:
            list(qwen.chat_completion_stream([{"role": "user", "content": "x"}]))
    assert ei.value.code == "llm_empty_response"


def test_context_cache_usage_supports_dict_and_missing_fields():
    """缓存用量兼容 DashScope 常见字段，缺失字段保持未知而不是伪造 0。"""
    usage = qwen.context_cache_usage(SimpleNamespace(usage={
        "input_tokens": 2048,
        "prompt_tokens_details": {
            "cached_tokens": 1024,
            "cache_creation_input_tokens": 0,
        },
    }))
    assert usage.input_tokens == 2048
    assert usage.cached_tokens == 1024
    assert usage.cache_creation_input_tokens == 0
    assert usage.cache_hit is True
    assert qwen.context_cache_usage(SimpleNamespace()).cache_hit is None


def test_context_cache_probe_keeps_query_after_cache_marker(monkeypatch):
    """P0 仅允许稳定前缀进入缓存块，动态问题必须作为后续 user 消息。"""
    monkeypatch.setattr(qwen.settings, "context_cache_poc_enabled", True)
    chunks = _stream_resp([
        SimpleNamespace(status_code=200, code="", message="", output={"text": "可以报名"}),
        SimpleNamespace(status_code=200, code="", message="", output={"text": ""}, usage={
            "input_tokens": 1500,
            "prompt_tokens_details": {"cached_tokens": 1100, "cache_creation_input_tokens": 0},
        }),
    ])
    with patch("app.llm.qwen.MultiModalConversation.call", return_value=chunks) as call:
        result = qwen.probe_context_cache("固定资料" * 400, "零基础可以报名吗？", "qwen-test")
    messages = call.call_args.kwargs["messages"]
    assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert messages[1]["content"] == [{"text": "零基础可以报名吗？"}]
    assert result.answer == "可以报名"
    assert result.usage.cached_tokens == 1100
    assert result.usage.cache_hit is True
    assert result.ttft_seconds is not None


def test_context_cache_probe_requires_explicit_switch(monkeypatch):
    monkeypatch.setattr(qwen.settings, "context_cache_poc_enabled", False)
    with pytest.raises(LLMError) as exc:
        qwen.probe_context_cache("固定资料", "问题")
    assert exc.value.code == "context_cache_poc_disabled"
