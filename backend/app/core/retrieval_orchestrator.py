"""复杂问题检索编排：查询规划 → 子问题全局/主题双路召回 → 覆盖保护。

开关全部关闭时由 rag_pipeline 直接调用旧 retriever，本模块不会进入热路径。
模块内每个增强步骤均可空结果或降级，最终仍保留全局混合检索作为答案来源。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from app.config import settings
from app.core import retriever
from app.core.query_planner import QueryPlan, plan_query
from app.core.retriever import RetrievedChunk

logger = logging.getLogger(__name__)
RRF_K = 60


@dataclass
class OrchestratedRetrieval:
    chunks: list[RetrievedChunk]
    plan: QueryPlan

    @property
    def answer_outline(self) -> str:
        if not self.plan.is_multi:
            return ""
        lines = ["请按以下顺序分项回答。每一项只能依据相应参考资料；资料不足时明确说明知识库未找到该部分资料，不要编造："]
        lines.extend(f"{index}. {question}" for index, question in enumerate(self.plan.sub_queries, 1))
        return "\n".join(lines)


def _clone(chunk: RetrievedChunk, sub_index: int) -> RetrievedChunk:
    metadata = dict(chunk.metadata)
    metadata["sub_question_indexes"] = [sub_index]
    return RetrievedChunk(
        chunk_id=chunk.chunk_id,
        text=chunk.text,
        score=chunk.score,
        metadata=metadata,
        similarity=chunk.similarity,
    )


def _fuse_two_routes(global_chunks: list[RetrievedChunk], topic_chunks: list[RetrievedChunk], sub_index: int) -> list[RetrievedChunk]:
    """主题与全局两张排序表做二层 RRF；主题为空时自然退为全局表。"""
    scores: dict[str, float] = {}
    chunks: dict[str, RetrievedChunk] = {}
    for ranked in (global_chunks, topic_chunks):
        for rank, source in enumerate(ranked):
            chunk = _clone(source, sub_index)
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (RRF_K + rank)
            chunks[chunk.chunk_id] = chunk
    for chunk_id, score in scores.items():
        chunks[chunk_id].score = score
    return sorted(chunks.values(), key=lambda item: item.score, reverse=True)[: settings.hybrid_fusion_candidates]


def _query_limit_for_sub(index: int, count: int) -> int:
    """总查询数封顶：先保证每个子问题原句，再把剩余预算均匀给改写。"""
    total = max(count, settings.max_retrieval_queries)
    base, extra = divmod(total, count)
    return max(1, base + (1 if index < extra else 0))


def _retrieve_one(kb_id: str, sub_query: str, topics: list[str], sub_index: int, sub_count: int) -> list[RetrievedChunk]:
    # 全局路是可靠主路径；局部主题路失败时 topic_retriever 自己返回空。
    global_chunks = retriever.retrieve(
        kb_id,
        sub_query,
        top_k=settings.hybrid_fusion_candidates,
        max_tokens=settings.max_context_tokens * 4,
        apply_rerank=False,
        max_queries=_query_limit_for_sub(sub_index, sub_count),
    )
    topic_chunks: list[RetrievedChunk] = []
    if settings.topic_retrieval_enabled and topics:
        try:
            from app.core.topic_retriever import retrieve as topic_retrieve

            topic_chunks = topic_retrieve(kb_id, sub_query, topics)
        except Exception:
            logger.exception("主题路异常，保留全局路：sub=%s", sub_query[:40])
    return _fuse_two_routes(global_chunks, topic_chunks, sub_index)


def _merge_with_coverage(per_sub: list[list[RetrievedChunk]], plan: QueryPlan) -> list[RetrievedChunk]:
    """跨子问题合并：先覆盖每题，再按总分补齐候选。"""
    merged: dict[str, RetrievedChunk] = {}
    for sub_index, chunks in enumerate(per_sub):
        for chunk in chunks:
            old = merged.get(chunk.chunk_id)
            if old is None:
                merged[chunk.chunk_id] = chunk
            else:
                old.score += chunk.score
                old.metadata["sub_question_indexes"] = sorted(
                    set(old.metadata.get("sub_question_indexes", []))
                    | set(chunk.metadata.get("sub_question_indexes", []))
                )
    ranked = sorted(merged.values(), key=lambda item: item.score, reverse=True)
    selected: list[RetrievedChunk] = []
    selected_ids: set[str] = set()
    # 每个子问题至少一个候选；没有候选则由 Prompt 明确说明资料不足。
    for chunks in per_sub:
        for chunk in chunks[: settings.topic_coverage_per_sub_query]:
            if chunk.chunk_id not in selected_ids:
                selected.append(merged[chunk.chunk_id])
                selected_ids.add(chunk.chunk_id)
    for chunk in ranked:
        if len(selected) >= settings.hybrid_fusion_candidates:
            break
        if chunk.chunk_id not in selected_ids:
            selected.append(chunk)
            selected_ids.add(chunk.chunk_id)
    return selected


def _rerank_and_keep_coverage(query: str, candidates: list[RetrievedChunk], per_sub: list[list[RetrievedChunk]]) -> list[RetrievedChunk]:
    ranked = candidates
    if settings.rerank_enabled and len(candidates) > 1:
        try:
            from app.core.reranker import rerank

            ranked = rerank(query, candidates)
        except Exception:
            logger.exception("编排层重排异常，保留融合排序")
    # 重排后再次回填覆盖，避免热门主题挤掉其余子问题。
    required_ids = {chunks[0].chunk_id for chunks in per_sub if chunks}
    result: list[RetrievedChunk] = []
    seen: set[str] = set()
    for chunk in ranked:
        if chunk.chunk_id in required_ids and chunk.chunk_id not in seen:
            result.append(chunk)
            seen.add(chunk.chunk_id)
    for chunk in ranked:
        if chunk.chunk_id not in seen:
            result.append(chunk)
            seen.add(chunk.chunk_id)
    return result


def retrieve(kb_id: str, query: str, history_context: str = "") -> OrchestratedRetrieval:
    """增强检索总入口。规划失败时 plan_query 已退为单问题。"""
    plan = plan_query(query, history_context)
    sub_count = len(plan.sub_queries)
    try:
        per_sub: list[list[RetrievedChunk]] = [[] for _ in plan.sub_queries]
        with ThreadPoolExecutor(max_workers=min(3, sub_count), thread_name_prefix="planned-retrieve") as pool:
            futures = {
                pool.submit(_retrieve_one, kb_id, text, plan.topic_hints[index], index, sub_count): index
                for index, text in enumerate(plan.sub_queries)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    per_sub[index] = future.result()
                except Exception:
                    logger.exception("子问题检索失败，跳过该子问题：%s", plan.sub_queries[index][:40])
        candidates = _merge_with_coverage(per_sub, plan)
        ranked = _rerank_and_keep_coverage(query, candidates, per_sub)
        final_count = max(settings.retrieval_top_k, sum(bool(chunks) for chunks in per_sub))
        chunks = retriever.truncate_to_budget(ranked[:final_count], settings.max_context_tokens)
        logger.info("编排检索：mode=%s sub=%d candidates=%d final=%d", plan.mode, sub_count, len(candidates), len(chunks))
        return OrchestratedRetrieval(chunks, plan)
    except Exception:
        logger.exception("检索编排失败，回退现有混合检索：kb=%s", kb_id)
        return OrchestratedRetrieval(retriever.retrieve(kb_id, query), QueryPlan(query, "single", [query], [[]]))
