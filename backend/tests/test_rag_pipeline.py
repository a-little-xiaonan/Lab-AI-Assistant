"""RAG 主流程测试：mock LLM 与检索（无需 API key）。"""
from unittest.mock import patch

import pytest

from app.core import rag_pipeline
from app.core.retriever import RetrievedChunk
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
def test_answer_returns_sources_and_citations(mock_chat, mock_retrieve):
    result = rag_pipeline.answer("qwen-plus 怎么收费？", "kb_default")

    assert result["answer"].endswith("[来源: 手册.pdf P3]\n[来源: faq.md]")
    # 引用去重：同 (file, page) 只出现一次
    assert result["answer"].count("[来源: 手册.pdf P3]") == 1
    assert result["sources"][0]["source_file"] == "手册.pdf"
    assert result["sources"][0]["page"] == 3
    assert len(result["sources"]) == 2

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
def test_retrieval_failure_degrades_to_plain_llm(mock_chat, mock_retrieve):
    result = rag_pipeline.answer("问题", "kb_default")
    assert result["answer"] == "降级回答。"
    assert result["sources"] == []


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
    from app.core import rag_pipeline as rp

    with patch("app.core.rag_pipeline.retriever.retrieve", return_value=[]), patch(
        "app.core.rag_pipeline.qwen.chat_completion", return_value="ok"
    ) as mock_chat:
        rp.answer("第二个问题", "kb_default", history=[("user", "第一个问题"), ("assistant", "第一个回答")])
        user_content = mock_chat.call_args.args[0][1]["content"]
        assert "## 对话历史" in user_content
        assert "user: 第一个问题" in user_content
