"""检索器：向量检索（Phase 2 路径）→ 阈值过滤 → token 预算截断。

Phase 3-06 起增加 dispatcher：hybrid_retrieval_enabled=true 时转发到
hybrid_retriever（向量 ∥ BM25 → RRF 融合，阈值不再硬过滤只做观测）；
false 时本文件的 Phase 2 逻辑逐字节保留（回归契约）。
- similarity = 1 - distance（cosine 空间，已实测确认）
- 检索结果为空不硬凑（宁缺毋滥，模型会被 prompt 告知"无相关信息"）
- max_context_tokens：按分从高到低累积，超限截断（truncate_to_budget 公共函数）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import settings
from app.llm.qwen import embed_query
from app.store.vector_store import vector_store

logger = logging.getLogger(__name__)


@dataclass
class RetrievedChunk:
    chunk_id: str
    text: str
    score: float  # 余弦相似度（Phase 2 路径）/ RRF 分或重排分（混合路径），已过过滤
    metadata: dict
    source_file: str = ""
    page: int | None = None
    similarity: float | None = None  # 混合路径观测字段：向量相似度（可选）

    def __post_init__(self) -> None:
        self.source_file = self.metadata.get("source_file", "")
        self.page = self.metadata.get("page")


def estimate_tokens(text: str) -> int:
    """粗估算：中文 ≈ 1 字/token，英文 ≈ 1 token/4 字符（精确计数后置）。"""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return cjk + (len(text) - cjk) // 4 + 1


def truncate_to_budget(
    chunks: list[RetrievedChunk], max_tokens: int
) -> list[RetrievedChunk]:
    """token 预算截断（保留至少 1 条）。Phase 2 与混合路径共用。"""
    kept: list[RetrievedChunk] = []
    used = 0
    for c in chunks:
        if kept and used + estimate_tokens(c.text) > max_tokens:
            break
        kept.append(c)
        used += estimate_tokens(c.text)
    return kept


def retrieve(
    kb_id: str,
    query_text: str,
    top_k: int | None = None,
    threshold: float | None = None,
    max_tokens: int | None = None,
) -> list[RetrievedChunk]:
    if settings.hybrid_retrieval_enabled:
        # 函数内懒导入：避免 retriever → hybrid_retriever → retriever 循环依赖
        from app.core.hybrid_retriever import retrieve as hybrid_retrieve

        return hybrid_retrieve(
            kb_id, query_text, top_k=top_k, max_tokens=max_tokens
        )

    # ===== Phase 2 路径（hybrid 关闭时逐字节保留）=====
    top_k = top_k if top_k is not None else settings.retrieval_top_k
    threshold = threshold if threshold is not None else settings.similarity_threshold
    max_tokens = max_tokens if max_tokens is not None else settings.max_context_tokens

    query_embedding = embed_query(query_text)
    hits = vector_store.query(kb_id, query_embedding, top_k)

    filtered: list[RetrievedChunk] = []
    for chunk_id, text, metadata, distance in hits:
        score = 1.0 - distance  # cosine：distance = 1 - 相似度
        if score >= threshold:
            filtered.append(RetrievedChunk(chunk_id=chunk_id, text=text, score=score, metadata=metadata))

    filtered.sort(key=lambda c: c.score, reverse=True)

    kept = truncate_to_budget(filtered, max_tokens)

    if not filtered:
        logger.info("检索无命中（阈值 %s 过滤）：kb=%s query=%s", threshold, kb_id, query_text[:40])
    return kept
