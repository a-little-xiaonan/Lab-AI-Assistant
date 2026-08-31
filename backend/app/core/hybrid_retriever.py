"""混合检索（Phase 3-06）：向量 ∥ BM25 关键词 → RRF 融合 → top-20 候选集。

- 向量侧：查询（单条；Phase 3-01 起支持多查询并发）→ embed → 各查 top-k → 按 chunk_id
  合并（score 取 max）→ 排序取 top-k 作为单条有序列表参与 RRF
- 关键词侧：默认只用原始问题（keyword_use_rewritten=true 时同多查询合并逻辑）
- RRF：score = Σ 1/(k + rank)，k=60；按 chunk_id（≡ (doc_id, chunk_index)）去重
- similarity_threshold 不再硬过滤（关键词独有命中不能被阈值误杀），只做观测日志
- 顺序：候选 top-20 → （Phase 3-02 重排，rerank_enabled）→ 取 top-n → token 预算截断

循环依赖规避：本模块顶层 import retriever（拿 RetrievedChunk/estimate_tokens）；
retriever 只在函数内懒导入本模块。jieba/rank_bm25 只在本依赖链出现。
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.config import settings
from app.core import retriever
from app.core.keyword_index import keyword_index
from app.core.retriever import RetrievedChunk
from app.llm import qwen
from app.store.vector_store import vector_store

logger = logging.getLogger(__name__)

RRF_K = 60  # RRF 平滑常数（文档公式固定值，不进 settings）
_QUERY_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="query-embed")

# 查询改写（Phase 3-01）：模块顶部一次尝试导入；01 之前文件不存在 → None，
# rewrite_enabled 即使为 true 也静默降级单查询（不每次抛异常）
try:
    from app.core.query_rewrite import rewrite as _rewrite
except ImportError:
    _rewrite = None


def _vector_hits_for_query(
    kb_id: str, query: str, top_k: int
) -> dict[str, tuple[float, str, dict]]:
    """单条查询的向量检索 → {chunk_id: (similarity, text, metadata)}。"""
    emb = qwen.embed_query(query)
    out: dict[str, tuple[float, str, dict]] = {}
    for chunk_id, text, metadata, distance in vector_store.query(kb_id, emb, top_k):
        sim = 1.0 - distance
        prev = out.get(chunk_id)
        if prev is None or sim > prev[0]:
            out[chunk_id] = (sim, text, metadata)
    return out


def _merge_query_results(
    kb_id: str, queries: list[str], top_k: int
) -> list[tuple[str, float, str, dict]]:
    """多查询并发向量检索 → 按 chunk_id 合并（similarity 取 max）→ 降序截 top_k。

    - 原查询（queries[0]）失败 → 向上抛（Phase 2 契约：_prepare 降级纯 LLM）
    - 改写查询失败 → 日志 + 丢该路（少一路检索，主链路不中断）
    """
    merged: dict[str, tuple[float, str, dict]] = {}
    futures = {_QUERY_POOL.submit(_vector_hits_for_query, kb_id, q, top_k): q for q in queries}
    for fut in as_completed(futures):
        q = futures[fut]
        try:
            hits = fut.result()
        except Exception:
            if q == queries[0]:
                raise
            logger.warning("改写查询检索失败，丢弃该路：%s", q[:40])
            continue
        for chunk_id, (sim, text, metadata) in hits.items():
            prev = merged.get(chunk_id)
            if prev is None or sim > prev[0]:
                merged[chunk_id] = (sim, text, metadata)
    ranked = sorted(merged.items(), key=lambda kv: kv[1][0], reverse=True)[:top_k]
    return [(cid, sim, text, md) for cid, (sim, text, md) in ranked]


def _keyword_hits(kb_id: str, query: str, top_k: int) -> list[tuple[str, float, str, dict]]:
    """BM25 关键词检索 → [(chunk_id, score, text, meta)]，meta 从索引取。"""
    out = []
    for chunk_id, score in keyword_index.search(kb_id, query, top_k):
        text = keyword_index.get_text(kb_id, chunk_id)
        meta = keyword_index.get_meta(kb_id, chunk_id)
        if text is not None and meta is not None:
            out.append((chunk_id, score, text, meta))
    return out


def retrieve(
    kb_id: str,
    query_text: str,
    top_k: int | None = None,
    max_tokens: int | None = None,
) -> list[RetrievedChunk]:
    """混合检索入口（由 retriever.retrieve dispatcher 转发）。

    返回按 RRF 分降序的候选集（fusion_candidates 上限），已做 token 预算截断。
    """
    vector_top_k = settings.hybrid_vector_top_k
    kw_top_k = settings.hybrid_keyword_top_k
    candidates_n = settings.hybrid_fusion_candidates

    # 查询列表：原查询 + 改写（Phase 3-01 接入；改写只喂向量侧）
    queries = [query_text]
    if settings.rewrite_enabled and _rewrite is not None:
        try:
            queries = _rewrite(query_text)
        except Exception:
            logger.exception("查询改写失败，降级为单查询")
            queries = [query_text]

    # 向量侧（多查询并发合并）
    vector_list = _merge_query_results(kb_id, queries, vector_top_k)
    vector_ids = {cid for cid, *_ in vector_list}

    # 关键词侧（默认只用原问题：词面精确优先）
    kw_query = query_text
    kw_list = _keyword_hits(kb_id, kw_query, kw_top_k)

    # RRF 融合（按 chunk_id 去重）
    rrf: dict[str, tuple[float, RetrievedChunk]] = {}
    for rank, (cid, sim, text, md) in enumerate(vector_list):
        s = 1.0 / (RRF_K + rank)
        rrf[cid] = (s, RetrievedChunk(
            chunk_id=cid, text=text, score=s, metadata=md, similarity=sim
        ))
    for rank, (cid, bm25_score, text, md) in enumerate(kw_list):
        s = 1.0 / (RRF_K + rank)
        prev = rrf.get(cid)
        if prev is None:
            rrf[cid] = (s, RetrievedChunk(chunk_id=cid, text=text, score=s, metadata=md))
        else:
            rrf[cid] = (prev[0] + s, prev[1])
            prev[1].score = prev[0] + s

    candidates = sorted(rrf.values(), key=lambda kv: kv[0], reverse=True)[:candidates_n]
    chunks = [c for _, c in candidates]

    # 观测日志：阈值不再硬过滤（关键词独有命中统计）
    kw_only = [c for c in chunks if c.chunk_id not in vector_ids]
    below = sum(1 for c in chunks if c.similarity is not None and c.similarity < settings.similarity_threshold)
    logger.info(
        "混合检索 kb=%s 候选=%d 关键词独有=%d 低于阈值(%.2f)=%d",
        kb_id, len(chunks), len(kw_only), settings.similarity_threshold, below,
    )

    # 重排（Phase 3-02 接入）：RRF 候选 → 精排 → 取 top-n
    if settings.rerank_enabled:
        from app.core.reranker import rerank  # 函数内导入：02 前文件不存在

        chunks = rerank(query_text, chunks)
        final_n = settings.rerank_top_n
    else:
        final_n = top_k if top_k is not None else settings.retrieval_top_k

    return retriever.truncate_to_budget(chunks[:final_n], max_tokens or settings.max_context_tokens)
