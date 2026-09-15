"""后台文档处理 worker（Phase 2-02）：上传立即返回，处理异步执行。

由 API 层 BackgroundTasks 调度；**自建数据库会话**（请求 session 已随响应关闭，
SQLAlchemy 的 session 不能跨线程/跨生命周期复用）。

流程：读记录 → 校验知识库仍在 → 解析分块 → 写入待审版本分块 → 质量检查。
审核前不写正式 ChromaDB 与 BM25，避免未发布资料被问答链路召回。
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import delete, select

from app.core import chunker, document_loader
from app.core.document_loader import DocumentParseError, UnsupportedFormatError
from app.core.retriever import estimate_tokens
from app.models.database import Document, DocumentVersion, DocumentVersionChunk, KnowledgeBase
from app.services.quality_checker import run_quality_checks
from app.store.db import SessionLocal

logger = logging.getLogger(__name__)


def _fail(db, doc: Document, message: str) -> None:
    version = db.get(DocumentVersion, doc.current_version_id) if doc.current_version_id else None
    if version:
        version.processing_status = "failed"
        version.review_status = "needs_change"
    # 已有正式版本时继续在线；只有首次上传失败才把整篇文档标为 failed。
    doc.status = "ready" if doc.published_version_id else "failed"
    if doc.published_version_id is None:
        doc.governance_status = "needs_change"
    doc.error_message = message
    db.commit()


def load_and_chunk(doc: Document) -> tuple[str, list]:
    """解析 + 分块（process_document 与 reindex 共享，禁止复制逻辑）。

    失败抛 UnsupportedFormatError/DocumentParseError（由调用方处理状态化）。
    """
    fmt, elements = document_loader.load(Path(doc.file_path))
    return fmt, chunker.chunk(elements, doc.id, doc.kb_id, doc.filename)


def process_document(doc_id: str, target_suffix: str = "docs") -> None:
    """后台处理单个文档。永不抛异常：失败一律状态化（failed + 可读错误），日志留痕。

    target_suffix 为旧调用兼容参数；新上传始终进入待审区，不直接写正式索引。
    """
    db = SessionLocal()
    try:
        doc = db.get(Document, doc_id)
        if doc is None:
            logger.warning("文档不存在，跳过后台处理：%s", doc_id)
            return
        version = db.get(DocumentVersion, doc.current_version_id) if doc.current_version_id else None
        if version is None:
            _fail(db, doc, "文档版本记录不存在")
            return
        if version.processing_status != "processing":
            logger.info("文档版本非 processing 状态，跳过：%s（%s）", doc_id, version.processing_status)
            return
        # 上传后知识库可能已被删除：worker 写入前重查，缩小删除/上传并发的窗口
        if db.get(KnowledgeBase, doc.kb_id) is None:
            _fail(db, doc, f"知识库不存在：{doc.kb_id}")
            return

        try:
            fmt, chunks = load_and_chunk(doc)
            logger.info("后台解析完成：%s fmt=%s %d chunks", doc.filename, fmt, len(chunks))
        except (UnsupportedFormatError, DocumentParseError) as exc:
            _fail(db, doc, str(exc))
            return

        # 草稿分块只写待审区；重新处理同一版本时覆盖，保证幂等。
        db.execute(delete(DocumentVersionChunk).where(
            DocumentVersionChunk.document_version_id == version.id
        ))
        db.add_all(
            DocumentVersionChunk(
                id=f"{version.id}_{c.chunk_index}", document_version_id=version.id,
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
        blocked = run_quality_checks(db, version, chunks)
        doc.status = "ready"
        if doc.published_version_id is None:
            doc.chunk_count = len(chunks)
        doc.error_message = "质量检查存在阻断项" if blocked else None
        version.processing_status = "ready"
        version.review_status = "needs_change" if blocked else "pending_review"
        version.chunk_count = len(chunks)
        if doc.published_version_id is None:
            doc.governance_status = version.review_status
        db.commit()
        logger.info("文档待审处理完成：%s chunk_count=%d blocked=%s", doc.filename, len(chunks), blocked)
        # AI 只生成待审核主题；失败不影响文档 ready，也不会影响全局检索。
        try:
            from app.core.topic_suggester import suggest_topics

            suggest_topics(db, doc, chunks)
        except Exception:
            logger.exception("主题初标调度失败：doc=%s", doc.id)
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
