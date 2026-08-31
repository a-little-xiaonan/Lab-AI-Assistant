"""文档上传与列表（Phase 1-04 简单实现；多知识库 CRUD 与删除在 Phase 2-02）。

上传流程：大小校验 → 内容 hash 去重 → 存盘 → 解析 → 分块 → 向量化入库。
任一环节失败：文档标记 failed（error_message 可读），上传流程不中断、不产生脏数据。
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.errors import BadRequestError, ConflictError, NotFoundError
from app.config import settings
from app.core import chunker, document_loader, embedder
from app.core.document_loader import DocumentParseError, UnsupportedFormatError
from app.core.retriever import estimate_tokens
from app.llm.errors import LLMError
from app.models.database import ChunkRecord, Document
from app.store.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])

KB_DEFAULT = "kb_default"


def _new_doc_id() -> str:
    return f"doc_{uuid4().hex[:12]}"


@router.post("/documents")
async def upload_document(
    file: UploadFile = File(...),
    knowledge_base_id: str = KB_DEFAULT,
    db: Session = Depends(get_db),
) -> dict:
    # 1. 大小校验
    content = await file.read()
    if len(content) > settings.max_upload_size_mb * 1024 * 1024:
        raise BadRequestError(
            "file_too_large", f"文件超过 {settings.max_upload_size_mb}MB 大小限制"
        )
    filename = file.filename or ""
    if not filename.strip():
        raise BadRequestError("empty_filename", "缺少文件名")

    # 2. 内容 hash 去重：仅已成功入库的文档拦截重传；
    #    旧记录是 failed（如无 API key 时代的失败）→ 清理后按新上传处理
    file_hash = hashlib.sha256(content).hexdigest()[:16]
    existing = db.scalar(
        select(Document).where(Document.file_hash == file_hash, Document.kb_id == knowledge_base_id)
    )
    if existing is not None and existing.status == "indexed":
        raise ConflictError(
            "duplicate_document",
            f"文件已存在：{existing.filename}（doc_id={existing.id}）",
        )
    if existing is not None:
        logger.info("清理 failed 旧记录后重传：%s（doc_id=%s）", existing.filename, existing.id)
        Path(existing.file_path).unlink(missing_ok=True)
        db.execute(delete(ChunkRecord).where(ChunkRecord.doc_id == existing.id))
        db.delete(existing)
        db.commit()

    # 3. 存盘（data/uploads/{doc_id}_{原始文件名}）
    doc_id = _new_doc_id()
    save_path = settings.uploads_dir / f"{doc_id}_{Path(filename).name}"
    save_path.write_bytes(content)
    doc = Document(
        id=doc_id,
        kb_id=knowledge_base_id,
        filename=filename,
        file_hash=file_hash,
        file_size=len(content),
        file_path=str(save_path),
    )
    db.add(doc)
    db.commit()

    # 4. 解析 + 分块（无需 API key）
    try:
        fmt, elements = document_loader.load(save_path)
        chunks = chunker.chunk(elements, doc_id, knowledge_base_id, filename)
        logger.info("解析完成 %s：fmt=%s，%d chunks", filename, fmt, len(chunks))
    except UnsupportedFormatError as exc:
        _fail(db, doc, str(exc))
        raise BadRequestError("unsupported_format", str(exc)) from exc
    except DocumentParseError as exc:
        _fail(db, doc, str(exc))
        raise BadRequestError("parse_failed", str(exc)) from exc

    # 5. 向量化入库（需要 API key）
    try:
        chunk_count = embedder.embed_and_store(knowledge_base_id, doc_id, chunks)
    except LLMError as exc:
        _fail(db, doc, exc.message)
        raise exc  # 统一 502 错误结构

    # 6. chunk 明细落库（与状态更新同一事务，保证与 ChromaDB 双写一致）
    db.add_all(
        ChunkRecord(
            id=f"{doc_id}_{c.chunk_index}",
            doc_id=doc_id,
            kb_id=knowledge_base_id,
            chunk_index=c.chunk_index,
            text=c.text,
            char_length=len(c.text),
            token_estimate=estimate_tokens(c.text),
            page=c.page,
            slide_number=c.slide_number,
            sheet_name=c.sheet_name,
            row_range=c.row_range,
        )
        for c in chunks
    )
    doc.status = "indexed"
    doc.chunk_count = chunk_count
    db.commit()
    return {
        "doc_id": doc_id,
        "filename": filename,
        "format": fmt,
        "file_size": len(content),
        "chunk_count": chunk_count,
        "status": "indexed",
    }


@router.get("/documents")
def list_documents(
    knowledge_base_id: str = KB_DEFAULT, db: Session = Depends(get_db)
) -> list[dict]:
    docs = db.scalars(
        select(Document)
        .where(Document.kb_id == knowledge_base_id)
        .order_by(Document.created_at.desc())
    )
    return [
        {
            "doc_id": d.id,
            "filename": d.filename,
            "file_size": d.file_size,
            "status": d.status,
            "error_message": d.error_message,
            "chunk_count": d.chunk_count,
            "created_at": d.created_at,
        }
        for d in docs
    ]


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


def _fail(db: Session, doc: Document, message: str) -> None:
    doc.status = "failed"
    doc.error_message = message
    db.commit()
