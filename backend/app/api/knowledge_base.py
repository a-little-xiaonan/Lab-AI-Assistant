"""知识库 CRUD（Phase 2-02）：建/列/详/删 + 文档级联删除。

- kb_default 为系统默认库（种子创建，禁止删除）
- documents.kb_id 无数据库外键（既有表无迁移框架），应用层操作前校验 + 显式级联
- 删除顺序固定（文档 02）：SQLite 记录 → Chroma collection → uploads 目录；
  SQLite 是唯一事务面，后两步失败记录日志（半删状态可由 collection/目录名重试清理）
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.errors import BadRequestError, ConflictError, NotFoundError
from app.config import settings
from app.models.database import ChunkRecord, Document, KnowledgeBase
from app.models.schemas import (
    DocumentOut,
    KnowledgeBaseCreate,
    KnowledgeBaseDetailOut,
    KnowledgeBaseOut,
)
from app.store.db import get_db
from app.store.vector_store import vector_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["knowledge-bases"])

KB_DEFAULT = "kb_default"


def _new_kb_id() -> str:
    return f"kb_{uuid4().hex[:12]}"


def _kb_out(db: Session, kb: KnowledgeBase) -> KnowledgeBaseOut:
    """列表/详情通用装配：附带文档与 chunk 统计（GROUP BY 一次查询带出）。"""
    rows = db.execute(
        select(Document.kb_id, func.count(Document.id), func.coalesce(func.sum(Document.chunk_count), 0))
        .group_by(Document.kb_id)
    ).all()
    stats = {r[0]: (int(r[1]), int(r[2])) for r in rows}
    doc_count, chunk_count = stats.get(kb.id, (0, 0))
    return KnowledgeBaseOut(
        id=kb.id,
        name=kb.name,
        description=kb.description,
        embedding_model=kb.embedding_model,
        document_count=doc_count,
        chunk_count=chunk_count,
        created_at=kb.created_at,
    )


@router.post("/knowledge-bases", response_model=KnowledgeBaseOut, status_code=201)
def create_knowledge_base(
    body: KnowledgeBaseCreate, db: Session = Depends(get_db)
) -> KnowledgeBaseOut:
    if db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == body.name)):
        raise ConflictError("duplicate_name", f"知识库名称已存在：{body.name}")
    if body.embedding_model is not None and body.embedding_model != settings.embedding_model:
        # 每库独立 embedding 模型后置（Phase 3-06 混合检索时评估）；先统一全局模型
        raise BadRequestError(
            "unsupported_embedding_model",
            f"当前仅支持全局 embedding 模型：{settings.embedding_model}",
        )
    kb = KnowledgeBase(
        id=_new_kb_id(),
        name=body.name,
        description=body.description,
        embedding_model=body.embedding_model or settings.embedding_model,
    )
    db.add(kb)
    db.commit()
    db.refresh(kb)
    return _kb_out(db, kb)


@router.get("/knowledge-bases", response_model=list[KnowledgeBaseOut])
def list_knowledge_bases(db: Session = Depends(get_db)) -> list[KnowledgeBaseOut]:
    return [_kb_out(db, kb) for kb in db.scalars(select(KnowledgeBase).order_by(KnowledgeBase.created_at))]


@router.get("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseDetailOut)
def get_knowledge_base(kb_id: str, db: Session = Depends(get_db)) -> KnowledgeBaseDetailOut:
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise NotFoundError("knowledge_base_not_found", f"知识库不存在：{kb_id}")
    out = _kb_out(db, kb)
    docs = db.scalars(
        select(Document).where(Document.kb_id == kb_id).order_by(Document.created_at.desc())
    )
    return KnowledgeBaseDetailOut(
        **out.model_dump(),
        documents=[
            DocumentOut(
                doc_id=d.id, filename=d.filename, file_size=d.file_size,
                status=d.status, error_message=d.error_message,
                chunk_count=d.chunk_count, created_at=d.created_at,
            )
            for d in docs
        ],
    )


@router.delete("/knowledge-bases/{kb_id}")
def delete_knowledge_base(kb_id: str, db: Session = Depends(get_db)) -> dict:
    """删除知识库：①SQLite 记录（事务面）→ ②Chroma collection → ③uploads 目录。

    documents.kb_id 无数据库外键（既有表无迁移框架），级联由本层显式执行：
    chunks → documents → knowledge_bases 同一事务删除。
    """
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise NotFoundError("knowledge_base_not_found", f"知识库不存在：{kb_id}")
    if kb_id == KB_DEFAULT:
        raise BadRequestError("default_kb_protected", "默认知识库不可删除")

    file_paths = [
        d.file_path
        for d in db.scalars(select(Document).where(Document.kb_id == kb_id))
    ]
    # ① SQLite：显式级联（chunks → documents → kb 同一事务）
    db.execute(delete(ChunkRecord).where(ChunkRecord.kb_id == kb_id))
    db.execute(delete(Document).where(Document.kb_id == kb_id))
    db.delete(kb)
    db.commit()

    # ② Chroma collection（幂等，缺 collection 吞异常）
    vector_store.delete_collection(kb_id)

    # ③ 原始文件：逐文件删（兼容 Phase 1 平铺遗留）+ 删除 kb 子目录
    for p in file_paths:
        Path(p).unlink(missing_ok=True)
    shutil.rmtree(settings.uploads_dir / kb_id, ignore_errors=True)

    return {"deleted": kb_id}
