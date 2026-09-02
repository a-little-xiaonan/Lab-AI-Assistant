"""RAG 主流程编排：检索 → 拼装 → 生成 → 输出净化。

answer()/answer_stream() 签名保持稳定：query / kb_id / session_id / **flags，
是后续 Query Rewrite（Phase 3-01）、Re-rank（3-02）、混合检索（3-06）、
长期记忆（3-03）的挂接点。

对话历史（Phase 2-03 起）由短期记忆产出：session_id 非空时从 memory_manager
取窗口（含摘要压缩），替代 Phase 1 的 history 透传参数（已退役）。

访客回答不展示引用编号、来源文件名或「参考来源」汇总；检索来源仍通过
sources 字段保留给后端调试使用，避免泄露内部资料名称。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.config import settings
from app.core import retriever
from app.llm import qwen
from app.llm.errors import LLMError
from app.llm.prompt_templates import (
    build_no_context_messages,
    build_rag_answer_messages,
    format_retrieved_chunks,
)
from app.memory.long_term import format_memories, long_term_memory
from app.memory.memory_manager import memory_manager

logger = logging.getLogger(__name__)

KB_DEFAULT = "kb_default"
SNIPPET_MAX = 50  # sources 片段摘要上限（文档 04 协议）


def _get_history_context(session_id: str | None) -> str:
    """从短期记忆取历史上下文（无 session_id → 空；记忆异常 → 降级为空不中断）。"""
    if not session_id:
        return ""
    try:
        mem = memory_manager.get(session_id)
        mem.trim_to_token_budget(settings.history_max_tokens)
        return mem.get_context()
    except Exception:
        logger.exception("短期记忆读取失败，本次不带历史：session=%s", session_id)
        return ""


def _dedup_sources(chunks: list[retriever.RetrievedChunk]) -> list[dict]:
    """按 (source_file, page) 去重，snippet 截断至 SNIPPET_MAX。"""
    seen = set()
    out = []
    for c in chunks:
        key = (c.source_file, c.page)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {"source_file": c.source_file, "page": c.page, "snippet": c.text[:SNIPPET_MAX]}
        )
    return out


def _retrieve_scope(kb_ids: list[str], query: str, history: str) -> tuple[list[retriever.RetrievedChunk], str]:
    """自动知识库范围检索：先在每库完成自身混合检索，再做跨库 RRF 融合。

    每个 chunk id 含 doc_id，跨库天然不冲突。每库异常只跳过该库，避免一个权限范围
    内的坏索引拖垮整个聊天请求。
    """
    if not kb_ids:
        return [], ""
    answer_outline = ""
    per_kb: dict[str, list[retriever.RetrievedChunk]] = {}

    def _one(kb_id: str):
        if settings.query_planning_enabled or settings.topic_retrieval_enabled:
            from app.core.retrieval_orchestrator import retrieve as orchestrated_retrieve

            result = orchestrated_retrieve(kb_id, query, history)
            return result.chunks, result.answer_outline
        return retriever.retrieve(kb_id, query), ""

    with ThreadPoolExecutor(max_workers=min(4, len(kb_ids)), thread_name_prefix="scope-retrieve") as pool:
        futures = {pool.submit(_one, kb_id): kb_id for kb_id in kb_ids}
        for future in as_completed(futures):
            kb_id = futures[future]
            try:
                chunks, outline = future.result()
                for chunk in chunks:
                    chunk.metadata = {**chunk.metadata, "knowledge_base_id": kb_id}
                per_kb[kb_id] = chunks
                if outline:
                    answer_outline = outline
            except Exception:
                logger.exception("知识库检索失败，跳过：kb=%s", kb_id)

    # 每库排序表均参与 RRF，不比较不同库的原始相似度/BM25 分数。
    scores: dict[str, float] = {}
    chunks_by_id: dict[str, retriever.RetrievedChunk] = {}
    for chunks in per_kb.values():
        for rank, chunk in enumerate(chunks):
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (60 + rank)
            chunks_by_id[chunk.chunk_id] = chunk
    merged = sorted(chunks_by_id.values(), key=lambda chunk: scores[chunk.chunk_id], reverse=True)
    for chunk in merged:
        chunk.score = scores[chunk.chunk_id]
    if settings.rerank_enabled and len(merged) > 1:
        try:
            from app.core.reranker import rerank

            merged = rerank(query, merged)
        except Exception:
            logger.exception("跨库重排失败，保留跨库 RRF 顺序")
    final = retriever.truncate_to_budget(merged[: settings.retrieval_top_k], settings.max_context_tokens)
    logger.info("自动范围检索：可读库=%d 命中=%d", len(kb_ids), len(final))
    return final, answer_outline


def _prepare(
    query: str, kb_id: str | list[str], history: str = "", user_id: str | None = None
) -> tuple[list[retriever.RetrievedChunk], list[dict]]:
    """检索 + 场景分支 + 长期记忆召回（answer 与 answer_stream 共享）。

    检索为空/全部低于阈值 → no_context 模板（无参考资料段，明确告知未找到）；
    检索失败降级纯 LLM（日志标记）。长期记忆（Phase 3-03）在两种场景都拼入，
    记忆召回失败返回空段，不阻断主链路。
    """
    answer_outline = ""
    try:
        if isinstance(kb_id, list):
            chunks, answer_outline = _retrieve_scope(kb_id, query, history)
        # 新编排仅在显式开启时接管；默认继续走原 retriever，保证当前稳定链路与测试契约不变。
        elif settings.query_planning_enabled or settings.topic_retrieval_enabled:
            from app.core.retrieval_orchestrator import retrieve as orchestrated_retrieve

            result = orchestrated_retrieve(kb_id, query, history)
            chunks = result.chunks
            answer_outline = result.answer_outline
        else:
            chunks = retriever.retrieve(kb_id, query)
    except Exception:
        logger.exception("检索失败，降级为纯 LLM 回答：kb=%s", kb_id)
        chunks = []
    memories = format_memories(long_term_memory.recall(query, user_id)) if user_id else ""
    if chunks:
        messages = build_rag_answer_messages(
            query,
            retrieved=format_retrieved_chunks(chunks),
            history=history,
            memories=memories,
            answer_outline=answer_outline,
        )
    else:
        messages = build_no_context_messages(query, history=history, memories=memories)
    return chunks, messages


def _strip_citations(body: str, *, trim: bool = True) -> str:
    """移除模型偶发输出的引用标记，避免把内部资料名称展示给访客。"""
    body = re.sub(r"\[(?:\d+|来源:\s*[^\]]+)\]", "", body)
    body = re.sub(r"（来源：[^）]+）", "", body)
    body = re.sub(r"[ \t]+\n", "\n", body)
    return body.strip() if trim else body


def _normalize_citations(
    raw_answer: str, chunks: list[retriever.RetrievedChunk]
) -> tuple[str, list[dict]]:
    """非流式输出净化：保留内部 sources，移除访客可见的引用文本。"""
    return _strip_citations(raw_answer), _dedup_sources(chunks) if chunks else []


class CitationStreamProcessor:
    """流式输出净化器：跨分片去除 [1]、[来源: 文件] 等引用标记。"""

    def __init__(self, chunks: list[retriever.RetrievedChunk]) -> None:
        self._tail = ""
        self._pattern = re.compile(r"\[(?:\d+|来源:\s*[^\]]+)\]")

    def feed(self, delta: str) -> str:
        """处理一段增量：尾部未闭合的引用标记保留到下个分片。"""
        self._tail += delta
        open_bracket = self._tail.rfind("[")
        if open_bracket != -1 and len(self._tail) - open_bracket <= 160:
            boundary = open_bracket
        else:
            boundary = len(self._tail)
        safe, self._tail = self._tail[:boundary], self._tail[boundary:]
        return self._replace(safe)

    def flush(self) -> str:
        out = self._replace(self._tail)
        self._tail = ""
        return out

    def _replace(self, text: str) -> str:
        return _strip_citations(self._pattern.sub("", text), trim=False)


def _finalize(
    raw_answer: str, chunks: list[retriever.RetrievedChunk]
) -> dict:
    body, sources = _normalize_citations(raw_answer, chunks)
    return {"answer": body, "sources": sources}


def answer(
    query: str,
    kb_id: str | list[str] = KB_DEFAULT,
    session_id: str | None = None,
    user_id: str | None = None,
    **flags,
) -> dict:
    """返回 {"answer": str, "sources": [{"source_file", "page", "snippet"}]}。

    异常路径：检索失败 → 降级为纯 LLM 回答（日志标记）；LLM 失败 → 抛 LLMError，
    由 API 层转统一错误（不静默返回空）。
    """
    chunks, messages = _prepare(query, kb_id, history=_get_history_context(session_id), user_id=user_id)
    raw_answer = qwen.chat_completion(messages)
    return _finalize(raw_answer, chunks)


def answer_stream(
    query: str,
    kb_id: str | list[str] = KB_DEFAULT,
    session_id: str | None = None,
    user_id: str | None = None,
    **flags,
) -> Iterator[dict]:
    """流式回答生成器：依次 yield 净化后的 delta，最后 yield done。

    LLM 流式失败 → 生成器向上抛 LLMError（由 API 层转 SSE error 帧）。
    """
    chunks, messages = _prepare(query, kb_id, history=_get_history_context(session_id), user_id=user_id)
    proc = CitationStreamProcessor(chunks)
    processed = []
    for delta in qwen.chat_completion_stream(messages):
        replaced = proc.feed(delta)
        if replaced:
            processed.append(replaced)
            yield {"type": "delta", "text": replaced}
    tail = proc.flush()
    if tail:
        processed.append(tail)
        yield {"type": "delta", "text": tail}

    body = _strip_citations("".join(processed))
    sources = _dedup_sources(chunks) if chunks else []
    yield {"type": "done", "full_text": body, "sources": sources}
