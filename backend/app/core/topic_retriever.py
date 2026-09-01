"""主题定向检索：在指定主题文档内执行向量 + BM25，再以 RRF 融合。

它始终只是全局混合检索的补充：任何异常或空主题集合返回空列表，由调用方保留
全局路结果。首期在应用层按 doc_id 过滤，避免依赖 ChromaDB 数组 metadata 的版本差异。
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.config import settings
from app.core.keyword_index import keyword_index
from app.core.retriever import RetrievedChunk
from app.core.term_aliases import term_aliases
from app.llm import qwen
from app.models.database import Document, DocumentTopic
from app.store.db import SessionLocal
from app.store.vector_store import vector_store

logger = logging.getLogger(__name__)
RRF_K = 60


def _topic_document_ids(kb_id: str, topic_codes: list[str]) -> set[str]:
    if not topic_codes:
        return set()
    with SessionLocal() as db:
        rows = db.scalars(
            select(DocumentTopic.doc_id)
            .join(Document, Document.id == DocumentTopic.doc_id)
            .where(
                Document.kb_id == kb_id,
                Document.status == "ready",
                DocumentTopic.topic_code.in_(topic_codes),
            )
        ).all()
    return set(rows)


def retrieve(kb_id: str, query_text: str, topic_codes: list[str], top_k: int | None = None) -> list[RetrievedChunk]:
    """主题候选检索。异常返回空，调用方不能把它当成主链路。"""
    try:
        doc_ids = _topic_document_ids(kb_id, topic_codes)
        if not doc_ids:
            logger.info("主题定向无可用文档：kb=%s topics=%s", kb_id, topic_codes)
            return []
        limit = top_k or settings.topic_retrieval_top_k
        # 多取一些后在应用层过滤，避免主题文档因全局排名靠后被过早截掉。
        raw_limit = max(limit * 5, 50)
        vector_hits: list[RetrievedChunk] = []
        embedding = qwen.embed_query(query_text)
        for chunk_id, text, metadata, distance in vector_store.query(kb_id, embedding, raw_limit):
            if metadata.get("doc_id") in doc_ids:
                vector_hits.append(
                    RetrievedChunk(chunk_id, text, 1.0 - distance, metadata, similarity=1.0 - distance)
                )
                if len(vector_hits) >= limit:
                    break

        keyword_scores: dict[str, tuple[float, str, dict]] = {}
        for expanded in term_aliases.expand(query_text):
            for chunk_id, score in keyword_index.search(kb_id, expanded, raw_limit):
                meta = keyword_index.get_meta(kb_id, chunk_id)
                text = keyword_index.get_text(kb_id, chunk_id)
                if meta is None or text is None or meta.get("doc_id") not in doc_ids:
                    continue
                previous = keyword_scores.get(chunk_id)
                if previous is None or score > previous[0]:
                    keyword_scores[chunk_id] = (score, text, meta)
        keyword_hits = [
            RetrievedChunk(chunk_id, text, score, metadata)
            for chunk_id, (score, text, metadata) in sorted(
                keyword_scores.items(), key=lambda item: item[1][0], reverse=True
            )[:limit]
        ]

        fused: dict[str, RetrievedChunk] = {}
        scores: dict[str, float] = {}
        for ranked in (vector_hits, keyword_hits):
            for rank, chunk in enumerate(ranked):
                score = scores.get(chunk.chunk_id, 0.0) + 1.0 / (RRF_K + rank)
                scores[chunk.chunk_id] = score
                fused[chunk.chunk_id] = chunk
                chunk.score = score
        result = sorted(fused.values(), key=lambda chunk: chunk.score, reverse=True)[:limit]
        logger.info("主题定向检索：kb=%s topics=%s docs=%d hits=%d", kb_id, topic_codes, len(doc_ids), len(result))
        return result
    except Exception:
        logger.exception("主题定向检索失败，返回空并由全局检索兜底：kb=%s", kb_id)
        return []
