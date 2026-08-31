"""系统统计：GET /api/stats（Phase 2-02 扩展：文档/chunk 总数 + 各库统计）。

旧键（document_count/chunk_count/storage_size/knowledge_base_count/vector_dim）
全部保留，新增 knowledge_bases 数组，兼容既有消费者。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.database import Document, KnowledgeBase
from app.store.db import get_db

router = APIRouter(tags=["system"])


@router.get("/stats")
def stats(db: Session = Depends(get_db)) -> dict:
    doc_count = db.scalar(select(func.count(Document.id))) or 0
    # MySQL 的 SUM 返回 Decimal，转 int 防 JSON 序列化成字符串
    chunk_count = int(db.scalar(select(func.coalesce(func.sum(Document.chunk_count), 0))) or 0)
    storage_size = int(db.scalar(select(func.coalesce(func.sum(Document.file_size), 0))) or 0)
    kb_count = db.scalar(select(func.count(KnowledgeBase.id))) or 0

    rows = db.execute(
        select(
            KnowledgeBase.id,
            KnowledgeBase.name,
            func.count(Document.id),
            func.coalesce(func.sum(Document.chunk_count), 0),
        )
        .outerjoin(Document, Document.kb_id == KnowledgeBase.id)
        .group_by(KnowledgeBase.id, KnowledgeBase.name)
        .order_by(KnowledgeBase.created_at)
    ).all()
    knowledge_bases = [
        {
            "id": r[0],
            "name": r[1],
            "document_count": int(r[2]),
            "chunk_count": int(r[3]),
        }
        for r in rows
    ]
    return {
        "document_count": doc_count,
        "chunk_count": chunk_count,
        "storage_size": storage_size,      # 上传文档总大小（字节）
        "knowledge_base_count": kb_count,
        "vector_dim": 1024,                # text-embedding-v3
        "knowledge_bases": knowledge_bases,  # Phase 2-02 新增：各库统计
    }
