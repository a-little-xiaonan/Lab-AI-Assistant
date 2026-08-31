"""向量化编排：chunk 列表 → 分批向量化 → 入库。"""
from __future__ import annotations

import logging

from app.llm.qwen import embed_texts
from app.store.vector_store import vector_store

logger = logging.getLogger(__name__)


def embed_and_store(kb_id: str, doc_id: str, chunks: list, progress_cb=None) -> int:
    """把文档的 chunk 全部向量化并写入向量库，返回 chunk 数。"""
    if not chunks:
        return 0
    texts = [c.text for c in chunks]

    def _log_progress(done: int, total: int) -> None:
        logger.info("向量化进度 %s：%d/%d", doc_id, done, total)
        if progress_cb:
            progress_cb(done, total)

    embeddings = embed_texts(texts, on_batch=_log_progress)
    vector_store.add_chunks(kb_id, chunks, embeddings)
    logger.info("入库完成 %s：%d chunks", doc_id, len(chunks))
    return len(chunks)
