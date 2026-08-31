"""RAG 主流程编排：检索 → 拼装 → 生成 → 引用规范化。

answer()/answer_stream() 签名保持稳定：query / kb_id / session_id / **flags，
是后续 Query Rewrite（Phase 3-01）、Re-rank（3-02）、混合检索（3-06）、
长期记忆（3-03）的挂接点。

对话历史（Phase 2-03 起）由短期记忆产出：session_id 非空时从 memory_manager
取窗口（含摘要压缩），替代 Phase 1 的 history 透传参数（已退役）。

引用规范化（Phase 2-04）：
- 正文 [n] 标记 → [来源: 文件名 P页码]（越界 n 剔除 + 日志）
- 模型直写 [来源: X] 幻觉校验：X 必须存在于检索结果（非流式路径严格剔除）
- 末尾「参考来源：」汇总段（同源合并、snippet ≤50 字；无引用回退检索列表）
- 流式路径 [n] 边流边替换（CitationStreamProcessor），直写幻觉无法追溯剔除，
  以日志 + done 帧 sources 权威兜底（与前端契约"以 done 帧 sources 为准"一致）
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterator

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


def _prepare(query: str, kb_id: str, history: str = "") -> tuple[list[retriever.RetrievedChunk], list[dict]]:
    """检索 + 场景分支 + 长期记忆召回（answer 与 answer_stream 共享）。

    检索为空/全部低于阈值 → no_context 模板（无参考资料段，明确告知未找到）；
    检索失败降级纯 LLM（日志标记）。长期记忆（Phase 3-03）在两种场景都拼入，
    记忆召回失败返回空段，不阻断主链路。
    """
    try:
        chunks = retriever.retrieve(kb_id, query)
    except Exception:
        logger.exception("检索失败，降级为纯 LLM 回答：kb=%s", kb_id)
        chunks = []
    memories = format_memories(long_term_memory.recall(query, kb_id))
    if chunks:
        messages = build_rag_answer_messages(
            query, retrieved=format_retrieved_chunks(chunks), history=history, memories=memories
        )
    else:
        messages = build_no_context_messages(query, history=history, memories=memories)
    return chunks, messages


def _build_reference_section(
    body: str, chunks: list[retriever.RetrievedChunk]
) -> tuple[str, list[dict]]:
    """汇总段 + sources（步骤 3-4）：正文有效引用（file,page）去重同源合并；
    无任何引用时回退检索结果去重列表（保住"始终有引用"的行为）。
    返回 (标注后全文, sources)。"""
    cited = set()
    for m in re.finditer(r"\[来源:\s*([^\]]+?)(?:\s+P(\d+))?\]", body):
        cited.add((m.group(1), int(m.group(2)) if m.group(2) else None))

    if cited:
        sources = []
        seen = set()
        for fname, page in cited:
            if (fname, page) in seen:
                continue
            seen.add((fname, page))
            snippet = next(
                (
                    c.text[:SNIPPET_MAX]
                    for c in chunks
                    if c.source_file == fname and c.page == page
                ),
                "",
            )
            sources.append({"source_file": fname, "page": page, "snippet": snippet})
    else:
        sources = _dedup_sources(chunks)

    if sources:
        entries = []
        for s in sources:
            loc = f" P{s['page']}" if s["page"] is not None else ""
            entries.append(f"- {s['source_file']}{loc}：{s['snippet']}")
        body += "\n\n参考来源：\n" + "\n".join(entries)
    return body, sources


def _normalize_citations(
    raw_answer: str, chunks: list[retriever.RetrievedChunk]
) -> tuple[str, list[dict]]:
    """非流式引用规范化（全量后处理，可严格校验）：
    ① [n] → [来源: ...]（越界剔除）② 直写 [来源: X] 幻觉校验 ③ 汇总段 + sources。
    """
    if not chunks:
        return raw_answer, []
    by_index = {i + 1: c for i, c in enumerate(chunks)}
    valid_files = {c.source_file for c in chunks}

    def _replace_index(m: re.Match) -> str:
        c = by_index.get(int(m.group(1)))
        if c is None:
            logger.warning("回答引用越界 [%s]，剔除", m.group(1))
            return ""
        loc = f" P{c.page}" if c.page is not None else ""
        return f"[来源: {c.source_file}{loc}]"

    body = re.sub(r"\[(\d+)\]", _replace_index, raw_answer)

    def _validate_direct(m: re.Match) -> str:
        name = m.group(1)
        if name in valid_files:
            return m.group(0)
        logger.warning("回答引用不存在的来源 %s，剔除（幻觉防护）", name)
        return ""

    body = re.sub(r"\[来源:\s*([^\]]+)\]", _validate_direct, body)
    return _build_reference_section(body, chunks)


class CitationStreamProcessor:
    """流式 [n] 替换器：保留尾部可能被跨块截断的 [n]，安全前缀即时替换。

    直写 [来源: X] 的幻觉校验在流路径无法追溯剔除（已下发），仅日志记录，
    权威性以 done 帧 sources 为准（文档 04 记录此不对称）。
    """

    def __init__(self, chunks: list[retriever.RetrievedChunk]) -> None:
        self._by_index = {i + 1: c for i, c in enumerate(chunks)}
        self._tail = ""
        self._pattern = re.compile(r"\[(\d+)\]")

    def feed(self, delta: str) -> str:
        """处理一段增量：返回可下发的替换后文本（尾部未闭合 [n] 保留到下块）。"""
        self._tail += delta
        open_bracket = self._tail.rfind("[")
        if open_bracket != -1 and len(self._tail) - open_bracket <= 6:
            boundary = open_bracket  # 可能是未完成的 [n]
        else:
            boundary = len(self._tail)
        safe, self._tail = self._tail[:boundary], self._tail[boundary:]
        return self._replace(safe)

    def flush(self) -> str:
        out = self._replace(self._tail)
        self._tail = ""
        return out

    def _replace(self, text: str) -> str:
        def _sub(m: re.Match) -> str:
            c = self._by_index.get(int(m.group(1)))
            if c is None:
                logger.warning("流式回答引用越界 [%s]，剔除", m.group(1))
                return ""
            loc = f" P{c.page}" if c.page is not None else ""
            return f"[来源: {c.source_file}{loc}]"

        return self._pattern.sub(_sub, text)


def _finalize(
    raw_answer: str, chunks: list[retriever.RetrievedChunk]
) -> dict:
    body, sources = _normalize_citations(raw_answer, chunks)
    return {"answer": body, "sources": sources}


def answer(
    query: str,
    kb_id: str = KB_DEFAULT,
    session_id: str | None = None,
    **flags,
) -> dict:
    """返回 {"answer": str, "sources": [{"source_file", "page", "snippet"}]}。

    异常路径：检索失败 → 降级为纯 LLM 回答（日志标记）；LLM 失败 → 抛 LLMError，
    由 API 层转统一错误（不静默返回空）。
    """
    chunks, messages = _prepare(query, kb_id, history=_get_history_context(session_id))
    raw_answer = qwen.chat_completion(messages)
    return _finalize(raw_answer, chunks)


def answer_stream(
    query: str,
    kb_id: str = KB_DEFAULT,
    session_id: str | None = None,
    **flags,
) -> Iterator[dict]:
    """流式回答生成器：依次 yield {"type": "delta", "text": 替换后增量}，
    生成结束后 yield 汇总段（最后一个 delta），最后 yield {"type": "done",
    "full_text", "sources"}。done.full_text 与已产出的 delta 拼接逐字一致。

    LLM 流式失败 → 生成器向上抛 LLMError（由 API 层转 SSE error 帧）。
    """
    chunks, messages = _prepare(query, kb_id, history=_get_history_context(session_id))
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

    # 汇总段 + sources（[n] 已流式替换，这里只做步骤 3-4）
    body, sources = _build_reference_section("".join(processed), chunks)
    citation_block = body[len("".join(processed)):]
    if citation_block:
        yield {"type": "delta", "text": citation_block}
    yield {"type": "done", "full_text": body, "sources": sources}
