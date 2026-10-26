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
import time
from dataclasses import dataclass
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.config import settings
from app.core.agent.runtime import current_budget, record_failure, submit_context, timeout_seconds
from app.core.retrieval import retriever
from app.core.retrieval.ranking.evidence_gate import EvidenceDecision, filter_evidence
from app.llm import qwen
from app.llm.errors import LLMError
from app.llm.prompt_templates import (
    RAG_PROMPT_VERSION,
    build_faq_answer_messages,
    build_no_context_messages,
    build_rag_answer_messages,
    format_retrieved_chunks,
)
from app.memory.long_term import format_memories, long_term_memory
from app.memory.memory_manager import memory_manager
from app.middleware.request_context import current_request_id
from app.observability.prompt import record_prompt_build
from app.observability.tracing import bounded_ids, is_truncated, stage, stage_for

logger = logging.getLogger(__name__)

KB_DEFAULT = "kb_default"
SNIPPET_MAX = 50  # sources 片段摘要上限（文档 04 协议）
NORMALIZE_VERSION = "citation-strip-v1"
KNOWLEDGE_UNAVAILABLE = "知识库查询暂时失败，请稍后重试。"


def _get_history_context(session_id: str | None) -> str:
    """从短期记忆取历史上下文（无 session_id → 空；记忆异常 → 降级为空不中断）。"""
    with stage("history.load") as observed:
        if not session_id:
            observed.finish("skipped", reason="no_session")
            return ""
        try:
            mem = memory_manager.get(session_id)
            before = mem.get_context()
            dropped = mem.trim_to_token_budget(settings.history_max_tokens)
            history = mem.get_context()
            observed.set_attributes(history_before_chars=len(before), history_chars=len(history),
                                    history_dropped_messages=dropped,
                                    history_messages=len(mem.messages), history_summary_present=bool(mem.summary))
            return history
        except Exception:
            observed.finish("degraded", error_code="history_read_failed")
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


def _provenance_refs(chunks: list[retriever.RetrievedChunk]) -> list[dict]:
    """只供服务端依据持久化；不含正文、Prompt 或临时资源句柄。"""
    return [
        {
            "type": "knowledge",
            "knowledge_base_id": chunk.metadata.get("knowledge_base_id"),
            "doc_id": chunk.metadata.get("doc_id"),
            "document_version_id": chunk.metadata.get("document_version_id"),
            "chunk_id": chunk.chunk_id,
            "chunk_index": chunk.metadata.get("chunk_index"),
            "page": chunk.page,
        }
        for chunk in chunks
    ]


def _retrieve_scope(kb_ids: list[str], query: str, history: str) -> tuple[list[retriever.RetrievedChunk], str, int]:
    """自动知识库范围检索：先在每库完成自身混合检索，再做跨库 RRF 融合。

    每个 chunk id 含 doc_id，跨库天然不冲突。每库异常只跳过该库，避免一个权限范围
    内的坏索引拖垮整个聊天请求。
    """
    if not kb_ids:
        return [], "", 0
    answer_outline = ""
    per_kb: dict[str, list[retriever.RetrievedChunk]] = {}
    failed_count = 0

    def _one(kb_id: str):
        if settings.query_planning_enabled or settings.topic_retrieval_enabled:
            from app.core.retrieval.retrieval_orchestrator import retrieve as orchestrated_retrieve

            result = orchestrated_retrieve(kb_id, query, history)
            return result.chunks, result.answer_outline
        return retriever.retrieve(kb_id, query), ""

    with ThreadPoolExecutor(max_workers=min(4, len(kb_ids)), thread_name_prefix="scope-retrieve") as pool:
        futures = {submit_context(pool, _one, kb_id): kb_id for kb_id in kb_ids}
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
                failed_count += 1
                record_failure("knowledge_retrieval_failed")
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
            from app.core.retrieval.ranking.reranker import rerank

            merged = rerank(query, merged)
        except Exception:
            logger.exception("跨库重排失败，保留跨库 RRF 顺序")
    final = retriever.truncate_to_budget(merged[: settings.retrieval_top_k], settings.max_context_tokens)
    logger.info("自动范围检索：可读库=%d 成功库=%d 失败库=%d 命中=%d",
                len(kb_ids), len(per_kb), failed_count, len(final))
    return final, answer_outline, failed_count


@dataclass
class KnowledgeEvidence:
    chunks: list[retriever.RetrievedChunk]
    candidates: list[retriever.RetrievedChunk]
    decision: EvidenceDecision
    answer_outline: str = ""
    status: str = "no_data"
    failures: tuple[str, ...] = ()


@dataclass
class FaqPrepared:
    """FAQ 命中后的稳定消息与已核验证据。"""

    code: str
    chunks: list[retriever.RetrievedChunk]
    messages: list[dict]
    cache_namespace: str
    template_version: int
    cache_enabled: bool


def _prepare_public_faq(
    query: str, kb_id: str | list[str], history: str, audience_scope: str
) -> FaqPrepared | None:
    """构造已校验 FAQ Prompt；未放量、未命中或异常均回退常规 RAG。"""
    if not settings.faq_template_enabled:
        return None
    readable_kb_ids = kb_id if isinstance(kb_id, list) else [kb_id]
    try:
        from app.core.faq.service import is_rollout_eligible, match_faq

        if not is_rollout_eligible(current_request_id(), settings.faq_template_rollout_percent):
            return None

        matched = match_faq(query, readable_kb_ids, audience_scope)
        if matched is None:
            return None
        if settings.faq_template_public_only and matched.audience_scope != "guest":
            return None
        bounded_history = history[-settings.faq_template_history_max_chars:]
        cache_enabled = settings.context_cache_enabled
        return FaqPrepared(
            code=matched.code,
            chunks=matched.chunks,
            messages=build_faq_answer_messages(
                matched.stable_prefix, query, bounded_history, use_context_cache=cache_enabled,
            ),
            cache_namespace=matched.cache_namespace,
            template_version=matched.template_version,
            cache_enabled=cache_enabled,
        )
    except Exception:
        logger.exception("FAQ 模板校验失败，回退常规 RAG")
        return None


def _log_faq_observation(
    faq: FaqPrepared, *, stage: str, route_elapsed: float, generation_elapsed: float | None = None,
    ttft_elapsed: float | None = None,
) -> None:
    """记录可聚合的 FAQ 指标，严禁写入问题、证据正文和完整回答。"""
    fields = [
        f"template={faq.code}",
        f"version={faq.template_version}",
        f"namespace={faq.cache_namespace}",
        f"cache_enabled={str(faq.cache_enabled).lower()}",
        f"stage={stage}",
        f"route_ms={route_elapsed * 1000:.1f}",
    ]
    if generation_elapsed is not None:
        fields.append(f"generation_ms={generation_elapsed * 1000:.1f}")
    if ttft_elapsed is not None:
        fields.append(f"ttft_ms={ttft_elapsed * 1000:.1f}")
    logger.info("FAQ 缓存观测：%s", " ".join(fields))


def prepare_evidence(query: str, kb_id: str | list[str], history: str = "", *,
                     strict: bool = False) -> KnowledgeEvidence:
    """只检索及核验证据，不读取个人记忆、不生成答案。"""
    answer_outline = ""
    failures = []
    budget = current_budget.get()
    failure_start = len(budget.failures) if budget else 0
    try:
        timeout_seconds(settings.llm_timeout)
        if isinstance(kb_id, list):
            chunks, answer_outline, failed_count = _retrieve_scope(kb_id, query, history)
            if failed_count:
                failures.append("knowledge_retrieval_failed")
                failures.append("knowledge_retrieval_all_failed" if failed_count == len(kb_id)
                                else "knowledge_retrieval_partial_failed")
        # 新编排仅在显式开启时接管；默认继续走原 retriever，保证当前稳定链路与测试契约不变。
        elif settings.query_planning_enabled or settings.topic_retrieval_enabled:
            from app.core.retrieval.retrieval_orchestrator import retrieve as orchestrated_retrieve

            result = orchestrated_retrieve(kb_id, query, history)
            chunks = result.chunks
            answer_outline = result.answer_outline
        else:
            chunks = retriever.retrieve(kb_id, query)
        if isinstance(kb_id, str):
            for chunk in chunks:
                chunk.metadata = {**chunk.metadata, "knowledge_base_id": kb_id}
        if strict:
            # 编排中的主题路也必须终检，不能仅依赖全局路过滤。
            scope = kb_id if isinstance(kb_id, list) else [kb_id]
            checked = []
            for current in scope:
                group = [c for c in chunks if c.metadata.get("knowledge_base_id", current) == current]
                checked.extend(retriever.filter_published(current, group))
            chunks = checked
    except Exception:
        logger.exception("知识检索失败，本次无可用证据：kb=%s", kb_id)
        failures.append("knowledge_retrieval_failed")
        chunks = []
    timeout_seconds(settings.llm_timeout)
    # 已通过 ACL、发布状态与有效期终检的候选可仅作为“展开定位”，不能直接回答。
    candidates = list(chunks)
    candidate_ids = [chunk.chunk_id for chunk in candidates]
    with stage_for(
        "retrieval", "evidence.gate",
        candidate_count=len(candidates),
        candidate_ids=bounded_ids(candidate_ids),
        truncated=is_truncated(candidate_ids),
    ) as observed:
        chunks, evidence = filter_evidence(query, chunks)
        reason = evidence.reason
        observed.set_attributes(
            **evidence.as_dict(),
            accepted=evidence.accepted,
            gate_reason=reason,
            supporting_chunks=evidence.supporting_chunks,
            total_chunks=evidence.total_chunks,
            judge_used=evidence.judge_used,
            fail_open="open" in reason,
            fail_closed="closed" in reason,
            evidence_ids=bounded_ids([chunk.chunk_id for chunk in chunks]),
        )
    if "failed" in evidence.reason:
        failures.append("evidence_verification_failed")
        if strict:
            chunks = []
    if budget:
        failures.extend(budget.failures[failure_start:])
    status = "ok" if chunks else "failed" if failures else "no_data"
    return KnowledgeEvidence(chunks, candidates, evidence, answer_outline, status, tuple(set(failures)))


def _prepare(
    query: str, kb_id: str | list[str], history: str = "", user_id: str | None = None,
    history_message_ids=(),
) -> tuple[list[retriever.RetrievedChunk], list[dict], EvidenceDecision, str]:
    prepared = prepare_evidence(query, kb_id, history)
    chunks, evidence, answer_outline = prepared.chunks, prepared.decision, prepared.answer_outline
    if prepared.status == "failed":
        return [], [], evidence, prepared.status
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
    record_prompt_build(
        "answer_rag",
        messages,
        template_version=RAG_PROMPT_VERSION,
        history_message_ids=history_message_ids,
        evidence_ids=[chunk.chunk_id for chunk in chunks],
        budget_tokens=settings.max_context_tokens,
    )
    return chunks, messages, evidence, prepared.status


def _faq_evidence(chunks: list[retriever.RetrievedChunk]) -> EvidenceDecision:
    """FAQ 证据已在模板匹配阶段逐项校验，诊断中仍保留统一结构。"""
    return EvidenceDecision(
        accepted=True,
        reason="faq_template_verified",
        supporting_chunks=len(chunks),
        total_chunks=len(chunks),
        max_similarity=None,
        max_lexical_coverage=1.0,
        max_rerank_score=None,
        judge_used=False,
    )


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
    raw_answer: str, chunks: list[retriever.RetrievedChunk], answer_mode: str = "rag"
) -> dict:
    with stage("output.normalize", input_chars=len(raw_answer),
               processor_version=NORMALIZE_VERSION) as observed:
        body, sources = _normalize_citations(raw_answer, chunks)
        observed.set_attributes(output_chars=len(body), normalized=body != raw_answer,
                                source_count=len(sources), answer_mode=answer_mode)
    return {"answer": body, "sources": sources}


def answer(
    query: str,
    kb_id: str | list[str] = KB_DEFAULT,
    session_id: str | None = None,
    user_id: str | None = None,
    audience_scope: str = "guest",
    **flags,
) -> dict:
    """返回 {"answer": str, "sources": [{"source_file", "page", "snippet"}]}。

    异常路径：检索失败 → 明确告知服务失败；LLM 失败 → 抛 LLMError，
    由 API 层转统一错误（不静默返回空）。
    """
    started = time.monotonic()
    history = flags.get("history_context")
    history_message_ids = flags.get("history_message_ids", ())
    if history is None:
        history = _get_history_context(session_id)
    faq = _prepare_public_faq(query, kb_id, history, audience_scope)
    answer_mode = "rag"
    if faq is not None:
        try:
            chunks = faq.chunks
            evidence = _faq_evidence(chunks)
            status = "ok"
            answer_mode = "faq_context_cache" if faq.cache_enabled else "faq_template"
            record_prompt_build(
                "answer_faq", faq.messages,
                template_version=f"faq-v{faq.template_version}",
                history_message_ids=history_message_ids,
                evidence_ids=[chunk.chunk_id for chunk in chunks],
            )
            retrieval_elapsed = time.monotonic() - started
            generation_started = time.monotonic()
            raw_answer = (
                qwen.chat_completion_with_context_cache(faq.messages)
                if faq.cache_enabled else qwen.chat_completion(faq.messages, purpose="answer")
            )
            result = _finalize(raw_answer, chunks, answer_mode=answer_mode)
            _log_faq_observation(
                faq, stage="completed", route_elapsed=retrieval_elapsed,
                generation_elapsed=time.monotonic() - generation_started,
            )
        except LLMError:
            logger.exception("FAQ 模板调用失败，回退常规 RAG：template=%s", faq.code)
            _log_faq_observation(faq, stage="fallback", route_elapsed=time.monotonic() - started)
            faq = None
    if faq is None:
        chunks, messages, evidence, status = _prepare(
            query, kb_id, history=history, user_id=user_id,
            history_message_ids=history_message_ids,
        )
        retrieval_elapsed = time.monotonic() - started
        generation_started = time.monotonic()
        result = ({"answer": KNOWLEDGE_UNAVAILABLE, "sources": []} if status == "failed"
                  else _finalize(qwen.chat_completion(messages, purpose="answer"), chunks))
    if flags.get("include_diagnostics"):
        result["diagnostics"] = {
            "evidence": evidence.as_dict(),
            "status": status,
            "answer_mode": answer_mode,
            "retrieval_elapsed_seconds": round(retrieval_elapsed, 4),
            "generation_elapsed_seconds": round(
                time.monotonic() - generation_started, 4
            ),
            "retrieved": [
                {
                    "rank": index,
                    "chunk_id": chunk.chunk_id,
                    "source_file": chunk.source_file,
                    "score": round(float(chunk.score), 6),
                    "similarity": (
                        round(float(chunk.similarity), 6)
                        if chunk.similarity is not None
                        else None
                    ),
                    "rerank_score": (
                        round(float(chunk.rerank_score), 6)
                        if chunk.rerank_score is not None
                        else None
                    ),
                }
                for index, chunk in enumerate(chunks, 1)
            ],
        }
    if flags.get("include_provenance"):
        result["_evidence_refs"] = _provenance_refs(chunks)
    return result


def answer_stream(
    query: str,
    kb_id: str | list[str] = KB_DEFAULT,
    session_id: str | None = None,
    user_id: str | None = None,
    audience_scope: str = "guest",
    **flags,
) -> Iterator[dict]:
    """流式回答生成器：依次 yield 净化后的 delta，最后 yield done。

    LLM 流式失败 → 生成器向上抛 LLMError（由 API 层转 SSE error 帧）。
    """
    cancellation = flags.get("cancellation")
    if cancellation is not None and cancellation.is_set():
        return
    history = flags.get("history_context")
    history_message_ids = flags.get("history_message_ids", ())
    if history is None:
        history = _get_history_context(session_id)
    started = time.monotonic()
    faq = _prepare_public_faq(query, kb_id, history, audience_scope)
    if faq is not None:
        record_prompt_build(
            "answer_faq", faq.messages,
            template_version=f"faq-v{faq.template_version}",
            history_message_ids=history_message_ids,
            evidence_ids=[chunk.chunk_id for chunk in faq.chunks],
        )
        proc = CitationStreamProcessor(faq.chunks)
        processed = []
        emitted = False
        first_token_elapsed: float | None = None
        route_elapsed = time.monotonic() - started
        generation_started = time.monotonic()
        try:
            stream = (
                qwen.chat_completion_stream_with_context_cache(faq.messages)
                if faq.cache_enabled else qwen.chat_completion_stream(faq.messages, purpose="answer")
            )
            for delta in stream:
                if cancellation is not None and cancellation.is_set():
                    return
                replaced = proc.feed(delta)
                if replaced:
                    if first_token_elapsed is None:
                        first_token_elapsed = time.monotonic() - started
                    emitted = True
                    processed.append(replaced)
                    yield {"type": "delta", "text": replaced}
            tail = proc.flush()
            if tail:
                if first_token_elapsed is None:
                    first_token_elapsed = time.monotonic() - started
                emitted = True
                processed.append(tail)
                yield {"type": "delta", "text": tail}
            raw_body = "".join(processed)
            with stage("output.normalize", input_chars=len(raw_body),
                       processor_version=NORMALIZE_VERSION) as observed:
                body = _strip_citations(raw_body)
                observed.set_attributes(
                    output_chars=len(body), normalized=body != raw_body,
                    answer_mode="faq_context_cache" if faq.cache_enabled else "faq_template",
                )
            _log_faq_observation(
                faq, stage="completed", route_elapsed=route_elapsed,
                generation_elapsed=time.monotonic() - generation_started,
                ttft_elapsed=first_token_elapsed,
            )
            done = {"type": "done", "full_text": body, "sources": _dedup_sources(faq.chunks)}
            if flags.get("include_diagnostics"):
                done["diagnostics"] = {
                    "answer_mode": "faq_context_cache" if faq.cache_enabled else "faq_template",
                    "faq_template_code": faq.code,
                }
            if flags.get("include_provenance"):
                done["_evidence_refs"] = _provenance_refs(faq.chunks)
            yield done
            return
        except LLMError:
            if emitted:
                raise
            logger.exception("FAQ 模板流式调用失败，回退常规 RAG：template=%s", faq.code)
            _log_faq_observation(faq, stage="fallback", route_elapsed=time.monotonic() - started)

    chunks, messages, _evidence, status = _prepare(
        query, kb_id, history=history, user_id=user_id,
        history_message_ids=history_message_ids,
    )
    if cancellation is not None and cancellation.is_set():
        return
    if status == "failed":
        yield {"type": "delta", "text": KNOWLEDGE_UNAVAILABLE}
        done = {"type": "done", "full_text": KNOWLEDGE_UNAVAILABLE, "sources": []}
        if flags.get("include_diagnostics"):
            done["diagnostics"] = {"answer_mode": "rag"}
        yield done
        return
    proc = CitationStreamProcessor(chunks)
    processed = []
    for delta in qwen.chat_completion_stream(messages, purpose="answer"):
        if cancellation is not None and cancellation.is_set():
            return
        replaced = proc.feed(delta)
        if replaced:
            processed.append(replaced)
            yield {"type": "delta", "text": replaced}
    tail = proc.flush()
    if tail:
        processed.append(tail)
        yield {"type": "delta", "text": tail}

    raw_body = "".join(processed)
    with stage("output.normalize", input_chars=len(raw_body),
               processor_version=NORMALIZE_VERSION) as observed:
        body = _strip_citations(raw_body)
        observed.set_attributes(output_chars=len(body), normalized=body != raw_body,
                                answer_mode="rag")
    sources = _dedup_sources(chunks) if chunks else []
    done = {"type": "done", "full_text": body, "sources": sources}
    if flags.get("include_diagnostics"):
        done["diagnostics"] = {"answer_mode": "rag"}
    if flags.get("include_provenance"):
        done["_evidence_refs"] = _provenance_refs(chunks)
    yield done
