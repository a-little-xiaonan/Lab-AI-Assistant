"""后台文档处理 worker（Phase 2-02）：上传立即返回，处理异步执行。

由 API 层 BackgroundTasks 调度；**自建数据库会话**（请求 session 已随响应关闭，
SQLAlchemy 的 session 不能跨线程/跨生命周期复用）。

流程：读记录 → 校验知识库仍在 → 解析分块 → 向量化（全量成功才写库）→
chunk 明细与状态同事务提交。任何异常都不向接口抛：文档标记 failed + 可读
error_message，由前端轮询状态（文档 02 的状态机：processing → ready / failed）。
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import delete, select

from app.core import chunker, document_loader, embedder
from app.core.document_loader import DocumentParseError, UnsupportedFormatError
from app.core.retriever import estimate_tokens
from app.llm.errors import LLMError
from app.models.database import ChunkRecord, Document, KnowledgeBase
from app.store.db import SessionLocal
from app.store.vector_store import vector_store

logger = logging.getLogger(__name__)


def _fail(db, doc: Document, message: str) -> None:
    doc.status = "failed"
    doc.error_message = message
    db.commit()


def process_document(doc_id: str) -> None:
    """后台处理单个文档。永不抛异常：失败一律状态化（failed + 可读错误），日志留痕。"""
    db = SessionLocal()
    try:
        doc = db.get(Document, doc_id)
        if doc is None:
            logger.warning("文档不存在，跳过后台处理：%s", doc_id)
            return
        if doc.status != "processing":
            logger.info("文档非 processing 状态，跳过：%s（%s）", doc_id, doc.status)
            return
        # 上传后知识库可能已被删除：worker 写入前重查，缩小删除/上传并发的窗口
        if db.get(KnowledgeBase, doc.kb_id) is None:
            _fail(db, doc, f"知识库不存在：{doc.kb_id}")
            return

        try:
            fmt, elements = document_loader.load(Path(doc.file_path))
            chunks = chunker.chunk(elements, doc_id, doc.kb_id, doc.filename)
            logger.info("后台解析完成：%s fmt=%s %d chunks", doc.filename, fmt, len(chunks))
        except (UnsupportedFormatError, DocumentParseError) as exc:
            _fail(db, doc, str(exc))
            return

        try:
            chunk_count = embedder.embed_and_store(doc.kb_id, doc_id, chunks)
        except LLMError as exc:
            _fail(db, doc, exc.message)
            return
        except Exception:
            logger.exception("向量化未知异常，清理残留：doc=%s", doc_id)
            vector_store.delete_document(doc.kb_id, doc_id)  # 兜底清理可能半写入的向量
            _fail(db, doc, "向量化失败，请重试")
            return

        # chunk 明细 + 状态同事务提交（与 ChromaDB 双写一致，失败零脏数据）
        db.add_all(
            ChunkRecord(
                id=f"{doc_id}_{c.chunk_index}",
                doc_id=doc_id,
                kb_id=doc.kb_id,
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
        doc.status = "ready"
        doc.chunk_count = chunk_count
        db.commit()
        logger.info("文档处理完成：%s chunk_count=%d", doc.filename, chunk_count)
    except Exception:
        logger.exception("后台处理未知异常：doc=%s", doc_id)
        try:
            db.rollback()
            doc = db.get(Document, doc_id)
            if doc is not None:
                _fail(db, doc, "处理失败，请重试")
        except Exception:  # noqa: BLE001
            logger.exception("标记 failed 失败：doc=%s", doc_id)
    finally:
        db.close()
