"""检索器：向量检索 → 阈值过滤（业务层）→ token 预算截断。

- similarity_threshold=0.3 是业务规则：ChromaDB 只给原始 distance，这里换算
  similarity = 1 - distance（cosine 空间，已实测确认）后过滤
- 检索结果为空不硬凑（宁缺毋滥，模型会被 prompt 告知"无相关信息"）
- max_context_tokens=3000：按分从高到低累积，超限截断
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
    score: float  # 余弦相似度（0~1），已过阈值过滤
    metadata: dict
    source_file: str = ""
    page: int | None = None

    def __post_init__(self) -> None:
        self.source_file = self.metadata.get("source_file", "")
        self.page = self.metadata.get("page")


def estimate_tokens(text: str) -> int:
    """粗估算：中文 ≈ 1 字/token，英文 ≈ 1 token/4 字符（精确计数后置）。"""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return cjk + (len(text) - cjk) // 4 + 1


def retrieve(
    kb_id: str,
    query_text: str,
    top_k: int | None = None,
    threshold: float | None = None,
    max_tokens: int | None = None,
) -> list[RetrievedChunk]:
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

    # token 预算截断（保留至少 1 条）
    kept: list[RetrievedChunk] = []
    used = 0
    for c in filtered:
        if kept and used + estimate_tokens(c.text) > max_tokens:
            break
        kept.append(c)
        used += estimate_tokens(c.text)

    if not filtered:
        logger.info("检索无命中（阈值 %s 过滤）：kb=%s query=%s", threshold, kb_id, query_text[:40])
    return kept
