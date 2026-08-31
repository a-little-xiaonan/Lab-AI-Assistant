"""qwen.chat_completion_stream 单测：增量/差分兜底/首块重试/中断/空响应。

mock 方式与现有测试一致：patch 打在模块导入路径（app.llm.qwen.Generation.call）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from unittest.mock import patch

from app.llm import qwen
from app.llm.errors import LLMError


def _chunk(text: str, status: int = 200, code: str | None = None, message: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status, code=code, message=message, output=SimpleNamespace(text=text)
    )


def _stream_resp(chunks):
    return (c for c in chunks)


def test_stream_incremental_mode_yields_deltas():
    """incremental 形态（text 即增量）：直接产出。"""
    fake = _stream_resp(
        [_chunk("1"), _chunk("  \n2"), _chunk("3"), _chunk("")]
    )
    with patch("app.llm.qwen.Generation.call", return_value=fake):
        out = list(qwen.chat_completion_stream([{"role": "user", "content": "x"}]))
    assert out == ["1", "  \n2", "3"]


def test_stream_merge_mode_diffs_accumulated_text():
    """merge 形态（text 累积全文）：差分兜底只产出增量。"""
    fake = _stream_resp([_chunk("1"), _chunk("1  \n2"), _chunk("1  \n2  \n3"), _chunk("")])
    with patch("app.llm.qwen.Generation.call", return_value=fake):
        out = list(qwen.chat_completion_stream([{"role": "user", "content": "x"}]))
    assert out == ["1", "  \n2", "  \n3"]


def test_stream_mid_chunk_error_raises_interrupted():
    """中途块非 200 → llm_stream_interrupted（不重试，防重复输出）。"""
    fake = _stream_resp([_chunk("好的"), _chunk("", status=500, message="boom")])
    with patch("app.llm.qwen.Generation.call", return_value=fake):
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

    with patch("app.llm.qwen.Generation.call", side_effect=_side_effect):
        out = list(qwen.chat_completion_stream([{"role": "user", "content": "x"}]))
    assert calls["n"] == 2
    assert out == ["重试成功"]


def test_stream_retry_exhausted_raises():
    """首块持续 429 → 重试耗尽后抛 LLMError（llm_status_429）。

    注意用 side_effect：每次调用返回新生成器（模拟真实 SDK；return_value 会复用已耗尽的流）。
    """

    def _always_429(**kwargs):
        return _stream_resp([_chunk("", status=429)])

    with patch("app.llm.qwen.Generation.call", side_effect=_always_429):
        with pytest.raises(LLMError) as ei:
            list(qwen.chat_completion_stream([{"role": "user", "content": "x"}]))
    assert ei.value.code == "llm_status_429"


def test_stream_empty_output_raises():
    """全程空块（只有流结束标记）→ llm_empty_response（与非流式口径一致）。"""
    fake = _stream_resp([_chunk(""), _chunk("")])
    with patch("app.llm.qwen.Generation.call", return_value=fake):
        with pytest.raises(LLMError) as ei:
            list(qwen.chat_completion_stream([{"role": "user", "content": "x"}]))
    assert ei.value.code == "llm_empty_response"
