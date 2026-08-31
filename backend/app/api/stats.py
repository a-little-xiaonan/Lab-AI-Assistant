"""系统统计：GET /api/stats（MVP 先给真实的基础计数，图表展示在 Phase 3-04）"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.database import Document
from app.store.db import get_db

router = APIRouter(tags=["system"])


@router.get("/stats")
def stats(db: Session = Depends(get_db)) -> dict:
    doc_count = db.scalar(select(func.count(Document.id))) or 0
    # MySQL 的 SUM 返回 Decimal，转 int 防 JSON 序列化成字符串
    chunk_count = int(db.scalar(select(func.coalesce(func.sum(Document.chunk_count), 0))) or 0)
    storage_size = int(db.scalar(select(func.coalesce(func.sum(Document.file_size), 0))) or 0)
    return {
        "document_count": doc_count,
        "chunk_count": chunk_count,
        "storage_size": storage_size,      # 上传文档总大小（字节）
        "knowledge_base_count": 1,   # MVP 单知识库 kb_default
        "vector_dim": 1024,          # text-embedding-v3
    }
