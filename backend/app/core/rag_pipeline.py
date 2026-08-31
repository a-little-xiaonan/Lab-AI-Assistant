"""RAG 主流程编排：检索 → 拼装 → 生成 → 后处理。

answer() 签名保持稳定：query / kb_id / session_id / **flags，
是后续 Query Rewrite（Phase 3-01）、Re-rank（3-02）、混合检索（3-06）、
长期记忆（3-03）的挂接点。
"""
from __future__ import annotations

import logging

from app.core import retriever
from app.llm import qwen
from app.llm.errors import LLMError
from app.llm.prompt_templates import build_messages, format_history, format_retrieved_chunks

logger = logging.getLogger(__name__)

KB_DEFAULT = "kb_default"


def _dedup_sources(chunks: list[retriever.RetrievedChunk]) -> list[dict]:
    """按 (source_file, page) 去重，生成引用标注与结构化 sources。"""
    seen = set()
    out = []
    for c in chunks:
        key = (c.source_file, c.page)
        if key in seen:
            continue
        seen.add(key)
        out.append({"source_file": c.source_file, "page": c.page, "snippet": c.text[:100]})
    return out


def answer(
    query: str,
    kb_id: str = KB_DEFAULT,
    session_id: str | None = None,
    history: list[tuple[str, str]] | None = None,
    **flags,
) -> dict:
    """返回 {"answer": str, "sources": [{"source_file", "page", "snippet"}]}。

    异常路径：检索失败 → 降级为纯 LLM 回答（日志标记）；LLM 失败 → 抛 LLMError，
    由 API 层转统一错误（不静默返回空）。
    """
    history = history or []
    try:
        chunks = retriever.retrieve(kb_id, query)
    except Exception:
        logger.exception("检索失败，降级为纯 LLM 回答：kb=%s", kb_id)
        chunks = []

    messages = build_messages(
        query,
        retrieved=format_retrieved_chunks(chunks),
        history=format_history(history),
    )

    try:
        raw_answer = qwen.chat_completion(messages)
    except LLMError:
        raise

    sources = _dedup_sources(chunks)
    answer_text = raw_answer
    if sources:
        # 引用标注格式即协议：[来源: 产品手册.pdf P12]（Phase 2-04 细化规范）
        refs = []
        for s in sources:
            loc = f" P{s['page']}" if s["page"] is not None else ""
            refs.append(f"[来源: {s['source_file']}{loc}]")
        answer_text += "\n\n" + "\n".join(refs)
    return {"answer": answer_text, "sources": sources}
