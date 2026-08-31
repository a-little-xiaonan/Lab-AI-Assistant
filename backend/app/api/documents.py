"""文档上传/列表/删除（Phase 2-02：挂到知识库下，处理后台化）。

上传流程（立即返回 202 + processing，处理由 BackgroundTasks 异步执行）：
大小校验 → 同名去重 → 存盘 → 后台 worker 解析/分块/向量化/落库。
任一环节失败：文档标记 failed（error_message 可读），上传流程不中断、不产生脏数据。

旧端点 POST/GET /api/documents 保留为 deprecated 薄包装（scripts 兼容），
新前端一律使用 /api/knowledge-bases/{id}/documents。
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.errors import BadRequestError, ConflictError, NotFoundError
from app.config import settings
from app.core.document_processing import process_document
from app.core.retriever import estimate_tokens
from app.models.database import ChunkRecord, Document, KnowledgeBase
from app.models.schemas import DocumentOut, UploadDocumentOut
from app.store.db import get_db
from app.store.vector_store import vector_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])

KB_DEFAULT = "kb_default"


def _new_doc_id() -> str:
    return f"doc_{uuid4().hex[:12]}"


def _persist_upload(
    db: Session, content: bytes, filename: str, kb_id: str
) -> Document:
    """公共上传步骤：校验大小 → 同名去重（failed 旧记录清理重传）→ 存盘 → 登记。

    调用方负责在返回后调度后台处理（process_document）。
    """
    if len(content) > settings.max_upload_size_mb * 1024 * 1024:
        raise BadRequestError(
            "file_too_large", f"文件超过 {settings.max_upload_size_mb}MB 大小限制"
        )
    if not filename.strip():
        raise BadRequestError("empty_filename", "缺少文件名")

    # 同名去重：仅活跃（非 failed）文档拦截；failed 旧记录 → 清理后按新上传处理
    existing = db.scalar(
        select(Document).where(Document.kb_id == kb_id, Document.filename == filename)
    )
    if existing is not None and existing.status != "failed":
        raise ConflictError(
            "duplicate_document",
            f"同名文件已存在：{existing.filename}（doc_id={existing.id}）",
        )
    if existing is not None:
        logger.info("清理 failed 旧记录后重传：%s（doc_id=%s）", existing.filename, existing.id)
        Path(existing.file_path).unlink(missing_ok=True)
        db.execute(delete(ChunkRecord).where(ChunkRecord.doc_id == existing.id))
        vector_store.delete_document(kb_id, existing.id)
        db.delete(existing)
        db.commit()

    doc_id = _new_doc_id()
    save_dir = settings.uploads_dir / kb_id
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = save_dir / f"{doc_id}_{Path(filename).name}"
    save_path.write_bytes(content)
    doc = Document(
        id=doc_id,
        kb_id=kb_id,
        filename=filename,
        file_hash=hashlib.sha256(content).hexdigest()[:16],
        file_size=len(content),
        file_path=str(save_path),
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def _require_kb(db: Session, kb_id: str) -> KnowledgeBase:
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise NotFoundError("knowledge_base_not_found", f"知识库不存在：{kb_id}")
    return kb


@router.post("/knowledge-bases/{kb_id}/documents", response_model=UploadDocumentOut, status_code=202)
async def upload_document_to_kb(
    kb_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> UploadDocumentOut:
    """上传文档到指定知识库：立即返回 202 + processing，处理后台异步执行。"""
    _require_kb(db, kb_id)
    content = await file.read()
    doc = _persist_upload(db, content, file.filename or "", kb_id)
    background_tasks.add_task(process_document, doc.id)
    return UploadDocumentOut(
        doc_id=doc.id, filename=doc.filename, status="processing",
        file_size=doc.file_size, kb_id=kb_id,
    )


@router.get("/knowledge-bases/{kb_id}/documents", response_model=list[DocumentOut])
def list_kb_documents(kb_id: str, db: Session = Depends(get_db)) -> list[DocumentOut]:
    """知识库文档列表（含处理状态，前端轮询用）。"""
    _require_kb(db, kb_id)
    docs = db.scalars(
        select(Document)
        .where(Document.kb_id == kb_id)
        .order_by(Document.created_at.desc())
    )
    return [
        DocumentOut(
            doc_id=d.id,
            filename=d.filename,
            file_size=d.file_size,
            status=d.status,
            error_message=d.error_message,
            chunk_count=d.chunk_count,
            created_at=d.created_at,
        )
        for d in docs
    ]


@router.delete("/knowledge-bases/{kb_id}/documents/{doc_id}")
def delete_kb_document(kb_id: str, doc_id: str, db: Session = Depends(get_db)) -> dict:
    """删除文档：向量 + chunk 记录 + 登记 + 原文件。"""
    _require_kb(db, kb_id)
    doc = db.get(Document, doc_id)
    if doc is None or doc.kb_id != kb_id:
        raise NotFoundError("document_not_found", f"文档不存在：{doc_id}")

    db.execute(delete(ChunkRecord).where(ChunkRecord.doc_id == doc_id))
    vector_store.delete_document(kb_id, doc_id)
    Path(doc.file_path).unlink(missing_ok=True)
    db.delete(doc)
    db.commit()
    return {"deleted": doc_id}


# ===== 以下为 Phase 1 旧端点，保留为 deprecated 薄包装（scripts 兼容）=====

@router.post("/documents", response_model=UploadDocumentOut, status_code=202)
async def upload_document_deprecated(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    knowledge_base_id: str = KB_DEFAULT,
    db: Session = Depends(get_db),
) -> UploadDocumentOut:
    """[deprecated] 等价 POST /api/knowledge-bases/{kb}/documents（Phase 2 起处理后台化）。"""
    return await upload_document_to_kb(knowledge_base_id, background_tasks, file, db)


@router.get("/documents", response_model=list[DocumentOut])
def list_documents_deprecated(
    knowledge_base_id: str = KB_DEFAULT, db: Session = Depends(get_db)
) -> list[DocumentOut]:
    """[deprecated] 等价 GET /api/knowledge-bases/{kb}/documents。"""
    return list_kb_documents(knowledge_base_id, db)


@router.get("/documents/{doc_id}/chunks")
def list_chunks(
    doc_id: str,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> dict:
    """查看某文档的分块明细：内容、大小（字符数/token 估算）、位置元数据。"""
    doc = db.get(Document, doc_id)
    if doc is None:
        raise NotFoundError("document_not_found", f"文档不存在：{doc_id}")
    total = db.scalar(
        select(func.count(ChunkRecord.id)).where(ChunkRecord.doc_id == doc_id)
    ) or 0
    rows = db.scalars(
        select(ChunkRecord)
        .where(ChunkRecord.doc_id == doc_id)
        .order_by(ChunkRecord.chunk_index)
        .offset(offset)
        .limit(limit)
    )
    return {
        "doc_id": doc_id,
        "filename": doc.filename,
        "total": total,
        "offset": offset,
        "limit": limit,
        "chunks": [
            {
                "chunk_index": r.chunk_index,
                "text": r.text,
                "char_length": r.char_length,
                "token_estimate": r.token_estimate,
                "page": r.page,
                "slide_number": r.slide_number,
                "sheet_name": r.sheet_name,
                "row_range": r.row_range,
            }
            for r in rows
        ],
    }
