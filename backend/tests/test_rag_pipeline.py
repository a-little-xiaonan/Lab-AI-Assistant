"""RAG 主流程测试：mock LLM 与检索（无需 API key）。"""
from unittest.mock import patch

import pytest

from app.core import rag_pipeline
from app.core.faq.service import FaqMatch
from app.core.retrieval.retriever import RetrievedChunk
from app.llm.errors import LLMError


def _fake_chunks():
    return [
        RetrievedChunk(
            chunk_id="c1", text="qwen-plus 定价按 token 计费。",
            score=0.85, metadata={"doc_id": "d1", "source_file": "手册.pdf", "page": 3},
        ),
        RetrievedChunk(
            chunk_id="c2", text="qwen-plus 定价按 token 计费。",
            score=0.80, metadata={"doc_id": "d1", "source_file": "手册.pdf", "page": 3},
        ),
        RetrievedChunk(
            chunk_id="c3", text="千问支持流式输出。",
            score=0.70, metadata={"doc_id": "d2", "source_file": "faq.md"},
        ),
    ]


@patch("app.core.rag_pipeline.retriever.retrieve", return_value=_fake_chunks())
@patch("app.core.rag_pipeline.qwen.chat_completion", return_value="qwen-plus 按 token 计费。")
def test_answer_returns_internal_sources_without_visible_citations(mock_chat, mock_retrieve):
    result = rag_pipeline.answer("qwen-plus 怎么收费？", "kb_default")

    # 来源仅作为接口内部数据保留，回答正文不展示文件名或来源汇总。
    assert "参考来源：" not in result["answer"]
    assert "[来源:" not in result["answer"]
    assert result["sources"][0]["source_file"] == "手册.pdf"
    assert result["sources"][0]["page"] == 3
    assert len(result["sources"]) == 2
    assert all(len(s["snippet"]) <= 50 for s in result["sources"])  # snippet ≤50 协议

    # LLM 收到的 prompt 结构：system / user 含参考资料与问题
    messages = mock_chat.call_args.args[0]
    assert messages[0]["role"] == "system"
    user_content = messages[1]["content"]
    assert "## 参考资料" in user_content
    assert "qwen-plus 定价按 token 计费" in user_content
    assert "## 用户问题" in user_content
    assert "qwen-plus 怎么收费？" in user_content


@patch("app.core.rag_pipeline.retriever.retrieve", return_value=[])
@patch("app.core.rag_pipeline.qwen.chat_completion", return_value="我没有找到相关内容。")
def test_answer_no_hits_no_citation(mock_chat, mock_retrieve):
    result = rag_pipeline.answer("无关问题", "kb_default")
    assert result["sources"] == []
    assert "[来源:" not in result["answer"]
    user_content = mock_chat.call_args.args[0][1]["content"]
    assert "## 参考资料" not in user_content  # 无命中不塞空段


@patch("app.core.rag_pipeline.retriever.retrieve", side_effect=Exception("vector db down"))
@patch("app.core.rag_pipeline.qwen.chat_completion", return_value="降级回答。")
def test_retrieval_failure_reports_service_error(mock_chat, mock_retrieve):
    result = rag_pipeline.answer("问题", "kb_default")
    assert result["answer"] == rag_pipeline.KNOWLEDGE_UNAVAILABLE
    assert result["sources"] == []
    mock_chat.assert_not_called()


@patch("app.core.rag_pipeline.retriever.retrieve", side_effect=Exception("vector db down"))
def test_scope_retrieval_failure_is_not_reported_as_no_data(mock_retrieve):
    prepared = rag_pipeline.prepare_evidence("实验室报名条件", ["kb_a", "kb_b"])
    assert prepared.status == "failed"
    assert "knowledge_retrieval_failed" in prepared.failures
    assert "knowledge_retrieval_all_failed" in prepared.failures


@patch("app.core.rag_pipeline.retriever.retrieve", side_effect=Exception("vector db down"))
@patch("app.core.rag_pipeline.qwen.chat_completion_stream")
def test_retrieval_failure_stream_reports_service_error(mock_stream, mock_retrieve):
    items = list(rag_pipeline.answer_stream("实验室报名条件", ["kb_a"]))
    assert items[-1]["full_text"] == rag_pipeline.KNOWLEDGE_UNAVAILABLE
    assert items[-1]["sources"] == []
    mock_stream.assert_not_called()


@patch("app.core.rag_pipeline.retriever.retrieve", return_value=_fake_chunks())
@patch(
    "app.core.rag_pipeline.qwen.chat_completion",
    side_effect=LLMError("api_key_missing", "未配置 key"),
)
def test_llm_error_propagates(mock_chat, mock_retrieve):
    with pytest.raises(LLMError) as exc_info:
        rag_pipeline.answer("问题", "kb_default")
    assert exc_info.value.code == "api_key_missing"


def test_history_passed_into_prompt():
    """Phase 2-03：历史由短期记忆产出（history 参数已退役）。"""
    from app.core import rag_pipeline as rp
    from app.memory.short_term import ShortTermMemory

    mem = ShortTermMemory("sess_mem_test")
    mem.add_message("user", "第一个问题")
    mem.add_message("assistant", "第一个回答")

    with patch("app.core.rag_pipeline.retriever.retrieve", return_value=[]), patch(
        "app.core.rag_pipeline.qwen.chat_completion", return_value="ok"
    ) as mock_chat, patch("app.core.rag_pipeline.memory_manager.get", return_value=mem):
        rp.answer("第二个问题", "kb_default", session_id="sess_mem_test")
        user_content = mock_chat.call_args.args[0][1]["content"]
        assert "## 对话历史" in user_content
        assert "user: 第一个问题" in user_content


def test_no_session_id_means_no_history():
    """session_id 为空 → 不读记忆，prompt 无历史段。"""
    from app.core import rag_pipeline as rp

    with patch("app.core.rag_pipeline.retriever.retrieve", return_value=[]), patch(
        "app.core.rag_pipeline.qwen.chat_completion", return_value="ok"
    ) as mock_chat, patch("app.core.rag_pipeline.memory_manager.get") as mock_get:
        rp.answer("问题", "kb_default")
        mock_get.assert_not_called()
        user_content = mock_chat.call_args.args[0][1]["content"]
        assert "## 对话历史" not in user_content


@patch("app.core.rag_pipeline.retriever.retrieve", return_value=_fake_chunks())
@patch(
    "app.core.rag_pipeline.qwen.chat_completion_stream",
    return_value=iter(["你", "好", "！"]),
)
def test_answer_stream_emits_delta_and_done(mock_stream, mock_retrieve):
    """answer_stream：delta* → done，done.full_text 与已产出文本逐字一致。"""
    items = list(rag_pipeline.answer_stream("qwen-plus 怎么收费？", "kb_default"))
    types = [i["type"] for i in items]
    assert types == ["delta", "delta", "delta", "done"]
    full = "".join(i["text"] for i in items if i["type"] == "delta")
    done = items[-1]
    assert done["full_text"] == full  # 前端契约：done 与所见文本一致
    assert "参考来源：" not in done["full_text"]
    assert done["sources"][0]["source_file"] == "手册.pdf"
    assert done["sources"][0]["page"] == 3
    assert len(done["sources"]) == 2  # 同 (file,page) 去重


@patch("app.core.rag_pipeline.retriever.retrieve", return_value=[])
@patch(
    "app.core.rag_pipeline.qwen.chat_completion_stream",
    side_effect=LLMError("api_key_missing", "未配置 key"),
)
def test_answer_stream_llm_error_propagates(mock_stream, mock_retrieve):
    """流式 LLM 失败 → 生成器向上抛 LLMError（API 层转 error 帧），不产出 done。"""
    gen = rag_pipeline.answer_stream("问题", "kb_default")
    with pytest.raises(LLMError) as exc_info:
        next(gen)
    assert exc_info.value.code == "api_key_missing"


def _faq_match():
    return FaqMatch(
        code="recruit_deadline",
        audience_scope="guest",
        cache_namespace="namespace",
        stable_prefix="已审核 FAQ 证据卡",
        chunks=[
            RetrievedChunk(
                chunk_id="faq_1", text="报名截止时间为 10 月 20 日。", score=1.0,
                metadata={"doc_id": "doc_1", "source_file": "招新公告.md", "page": 1},
            )
        ],
    )


def test_answer_uses_verified_faq_context_cache(monkeypatch):
    """两个显式开关开启后，命中 FAQ 时不进入常规检索。"""
    monkeypatch.setattr(rag_pipeline.settings, "faq_template_enabled", True)
    monkeypatch.setattr(rag_pipeline.settings, "context_cache_enabled", True)
    with patch("app.core.faq.service.match_faq", return_value=_faq_match()), patch(
        "app.core.rag_pipeline.qwen.chat_completion_with_context_cache", return_value="截止时间为 10 月 20 日。"
    ) as cache_chat, patch("app.core.rag_pipeline.retriever.retrieve") as retrieve:
        result = rag_pipeline.answer("报名截止时间是什么时候？", ["kb_public"], include_diagnostics=True)

    assert result["answer"] == "截止时间为 10 月 20 日。"
    assert result["diagnostics"]["answer_mode"] == "faq_context_cache"
    assert result["diagnostics"]["evidence"]["reason"] == "faq_template_verified"
    cache_chat.assert_called_once()
    retrieve.assert_not_called()


def test_faq_cache_failure_falls_back_to_regular_rag(monkeypatch):
    monkeypatch.setattr(rag_pipeline.settings, "faq_template_enabled", True)
    monkeypatch.setattr(rag_pipeline.settings, "context_cache_enabled", True)
    fallback_chunks = _fake_chunks()
    fallback_evidence = rag_pipeline.EvidenceDecision(True, "fallback", 1, 1, 0.9, 1.0, None, False)
    with patch("app.core.faq.service.match_faq", return_value=_faq_match()), patch(
        "app.core.rag_pipeline.qwen.chat_completion_with_context_cache",
        side_effect=LLMError("context_cache_disabled", "缓存调用失败"),
    ), patch("app.core.rag_pipeline._prepare", return_value=(
        fallback_chunks, [{"role": "user", "content": "普通 RAG"}], fallback_evidence, "ok"
    )), patch("app.core.rag_pipeline.qwen.chat_completion", return_value="常规 RAG 回答") as normal_chat:
        result = rag_pipeline.answer("报名截止时间是什么时候？", ["kb_public"])

    assert result["answer"] == "常规 RAG 回答"
    normal_chat.assert_called_once()


def test_faq_cache_stream_failure_before_output_falls_back(monkeypatch):
    monkeypatch.setattr(rag_pipeline.settings, "faq_template_enabled", True)
    monkeypatch.setattr(rag_pipeline.settings, "context_cache_enabled", True)
    fallback_evidence = rag_pipeline.EvidenceDecision(True, "fallback", 1, 1, 0.9, 1.0, None, False)
    with patch("app.core.faq.service.match_faq", return_value=_faq_match()), patch(
        "app.core.rag_pipeline.qwen.chat_completion_stream_with_context_cache",
        side_effect=LLMError("llm_call_failed", "缓存调用失败"),
    ), patch("app.core.rag_pipeline._prepare", return_value=(
        _fake_chunks(), [{"role": "user", "content": "普通 RAG"}], fallback_evidence, "ok"
    )), patch("app.core.rag_pipeline.qwen.chat_completion_stream", return_value=iter(["常规", "回答"])):
        items = list(rag_pipeline.answer_stream("报名截止时间是什么时候？", ["kb_public"]))

    assert "".join(item["text"] for item in items if item["type"] == "delta") == "常规回答"
    assert items[-1]["type"] == "done"
