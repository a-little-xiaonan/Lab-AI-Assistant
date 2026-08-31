"""知识库 CRUD（Phase 2-02）+ 重新索引（Phase 3-05）。

- kb_default 为系统默认库（种子创建，禁止删除）
- documents.kb_id 无数据库外键（既有表无迁移框架），应用层操作前校验 + 显式级联
- 删除顺序固定（文档 02）：SQLite 记录 → Chroma collection → uploads 目录；
  SQLite 是唯一事务面，后两步失败记录日志（半删状态可由 collection/目录名重试清理）
- reindex（Phase 3-05）：双 buffer 重建，重复触发 409；详情见 app/core/reindex.py
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.errors import BadRequestError, ConflictError, NotFoundError
from app.config import settings
from app.core.reindex import reindex_manager
from app.models.database import ChunkRecord, Document, KnowledgeBase
from app.models.schemas import (
    DocumentOut,
    KnowledgeBaseCreate,
    KnowledgeBaseDetailOut,
    KnowledgeBaseOut,
    ReindexRequest,
    ReindexStatusOut,
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

    # ④ 关键词索引同步（缓存，异常不阻断）
    try:
        from app.core.keyword_index import keyword_index

        keyword_index.remove_kb(kb_id)
    except Exception:
        logger.exception("关键词索引同步失败（kb=%s）", kb_id)

    return {"deleted": kb_id}


# ===== 重新索引（Phase 3-05）=====

def _status_out(task) -> ReindexStatusOut:
    return ReindexStatusOut(
        kb_id=task.kb_id,
        doc_id=task.doc_id,
        status=task.status,
        total=task.total,
        done=task.done,
        current_doc=task.current_doc,
        docs_before=task.docs_before,
        docs_after=task.docs_after,
        error_message=task.error_message,
        started_at=task.started_at.isoformat() if task.started_at else None,
        finished_at=task.finished_at.isoformat() if task.finished_at else None,
    )


@router.post("/knowledge-bases/{kb_id}/reindex", response_model=ReindexStatusOut, status_code=202)
def reindex_knowledge_base(
    kb_id: str,
    background_tasks: BackgroundTasks,
    body: ReindexRequest | None = None,
    db: Session = Depends(get_db),
) -> ReindexStatusOut:
    """重建索引：单文档（body.doc_id）或全库（缺省）。重建期间检索不中断（双 buffer）。

    重复触发（同一 kb 任务运行中）→ 409 reindex_in_progress。
    """
    if db.get(KnowledgeBase, kb_id) is None:
        raise NotFoundError("knowledge_base_not_found", f"知识库不存在：{kb_id}")
    if reindex_manager.is_running(kb_id):
        raise ConflictError("reindex_in_progress", "该知识库正在重建索引，请稍后再试")
    task = reindex_manager.start(kb_id, body.doc_id if body else None)
    background_tasks.add_task(reindex_manager.run, kb_id, task.doc_id)
    return _status_out(task)


@router.get("/knowledge-bases/{kb_id}/reindex/status", response_model=ReindexStatusOut)
def reindex_status(kb_id: str, db: Session = Depends(get_db)) -> ReindexStatusOut:
    """重建进度（无任务 → status=idle）。"""
    if db.get(KnowledgeBase, kb_id) is None:
        raise NotFoundError("knowledge_base_not_found", f"知识库不存在：{kb_id}")
    task = reindex_manager.get(kb_id)
    if task is None:
        return ReindexStatusOut(kb_id=kb_id, status="idle")
    return _status_out(task)
