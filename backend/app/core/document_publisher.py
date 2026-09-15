"""审核发布器：在临时 collection 构建完整快照，成功后原子切换正式索引。"""
from __future__ import annotations

import logging

from sqlalchemy import delete, select

from app.core.embedder import embed_and_store
from app.core.models import Chunk
from app.models.database import (
    ChunkRecord, Document, DocumentVersion, DocumentVersionChunk, User, utcnow,
)
from app.services.audit import record_in_transaction
from app.services.document_governance import append_review
from app.store.db import SessionLocal
from app.store.vector_store import vector_store

logger = logging.getLogger(__name__)


def _chunk(doc: Document, row) -> Chunk:
    return Chunk(
        doc_id=doc.id, kb_id=doc.kb_id, source_file=doc.filename, text=row.text,
        chunk_index=row.chunk_index, page=row.page, slide_number=row.slide_number,
        sheet_name=row.sheet_name, row_range=row.row_range,
    )


def publish_document(doc_id: str, actor_id: str, expected_lock_version: int,
                     comment: str | None = None) -> None:
    """发布当前候选版本；任何失败均保留旧正式索引，并把原因写回文档。"""
    db = SessionLocal()
    doc: Document | None = None
    version: DocumentVersion | None = None
    try:
        doc = db.get(Document, doc_id)
        if doc is None or doc.deleted_at is not None:
            raise ValueError("文档不存在或已归档")
        if doc.lock_version != expected_lock_version:
            raise ValueError("文档版本已变化，请刷新后重新发布")
        version = db.get(DocumentVersion, doc.current_version_id)
        if version is None or version.review_status not in {"pending_review", "preapproved", "publishing"}:
            raise ValueError("当前版本不处于可发布状态")
        staged = list(db.scalars(select(DocumentVersionChunk).where(
            DocumentVersionChunk.document_version_id == version.id
        ).order_by(DocumentVersionChunk.chunk_index)))
        if not staged:
            raise ValueError("待发布版本没有可用分块")

        # 构建“其余已发布文档 + 当前候选版本”的完整快照。
        vector_store.create_temp_collection(doc.kb_id)
        live_docs = list(db.scalars(select(Document).where(
            Document.kb_id == doc.kb_id,
            Document.governance_status == "published",
            Document.deleted_at.is_(None),
            Document.id != doc.id,
        )))
        expected_chunk_count = len(staged)
        for live_doc in live_docs:
            rows = list(db.scalars(select(ChunkRecord).where(
                ChunkRecord.doc_id == live_doc.id
            ).order_by(ChunkRecord.chunk_index)))
            if rows:
                expected_chunk_count += len(rows)
                embed_and_store(live_doc.kb_id, live_doc.id,
                                [_chunk(live_doc, row) for row in rows], suffix="docs_new")
        candidate_chunks = [_chunk(doc, row) for row in staged]
        embed_and_store(doc.kb_id, doc.id, candidate_chunks, suffix="docs_new")
        expected_ids = {item.id for item in live_docs} | {doc.id}
        if vector_store.get_doc_ids(doc.kb_id, "docs_new") != expected_ids:
            raise ValueError("临时索引一致性校验失败")
        if vector_store.count(doc.kb_id, "docs_new") != expected_chunk_count:
            raise ValueError("临时索引分块数量校验失败")
        vector_store.swap_collections(doc.kb_id)

        # 向量快照切换成功后，事务性更新 SQL 正式分块和审核状态。
        db.execute(delete(ChunkRecord).where(ChunkRecord.doc_id == doc.id))
        db.add_all(ChunkRecord(
            id=f"{doc.id}_{row.chunk_index}", doc_id=doc.id,
            document_version_id=version.id, kb_id=doc.kb_id,
            chunk_index=row.chunk_index, text=row.text, char_length=row.char_length,
            token_estimate=row.token_estimate, page=row.page,
            slide_number=row.slide_number, sheet_name=row.sheet_name, row_range=row.row_range,
        ) for row in staged)
        old_version_id = doc.published_version_id
        if old_version_id and old_version_id != version.id:
            old_version = db.get(DocumentVersion, old_version_id)
            if old_version:
                old_version.review_status = "archived"
        now = utcnow()
        version.review_status = "published"
        doc.published_version_id = version.id
        doc.governance_status = "published"
        doc.published_by = actor_id
        doc.published_at = now
        doc.review_comment = comment
        doc.chunk_count = len(staged)
        doc.lock_version += 1
        append_review(db, version, actor_id, "publish", comment)
        record_in_transaction(db, db.get(User, actor_id),
                              "document.publish", "document", doc.id,
                              detail={"version_id": version.id, "kb_id": doc.kb_id})
        db.commit()
        try:
            from app.core.keyword_index import keyword_index
            keyword_index.rebuild_kb(doc.kb_id)
        except Exception:
            logger.exception("发布后关键词索引重建失败：kb=%s", doc.kb_id)
        logger.info("文档发布完成：doc=%s version=%s", doc.id, version.id)
    except Exception as exc:
        logger.exception("文档发布失败：doc=%s", doc_id)
        if doc is not None:
            vector_store.drop_temp_collection(doc.kb_id)
        db.rollback()
        doc = db.get(Document, doc_id)
        if doc:
            doc.error_message = f"发布失败：{exc}"
            failed_version = db.get(DocumentVersion, doc.current_version_id) if doc.current_version_id else None
            if failed_version and failed_version.review_status == "publishing":
                failed_version.review_status = "pending_review"
            if doc.published_version_id is None:
                doc.governance_status = "pending_review"
            db.commit()
    finally:
        db.close()


def archive_document(doc_id: str, actor_id: str, expected_lock_version: int,
                     comment: str | None = None) -> None:
    """归档文档并重建该库正式快照；旧版本和审核记录仍保留。"""
    db = SessionLocal()
    try:
        doc = db.get(Document, doc_id)
        if doc is None or doc.lock_version != expected_lock_version:
            raise ValueError("文档不存在或版本已变化")
        vector_store.create_temp_collection(doc.kb_id)
        live_docs = list(db.scalars(select(Document).where(
            Document.kb_id == doc.kb_id, Document.governance_status == "published",
            Document.deleted_at.is_(None), Document.id != doc.id,
        )))
        for live_doc in live_docs:
            rows = list(db.scalars(select(ChunkRecord).where(
                ChunkRecord.doc_id == live_doc.id
            ).order_by(ChunkRecord.chunk_index)))
            if rows:
                embed_and_store(live_doc.kb_id, live_doc.id,
                                [_chunk(live_doc, row) for row in rows], suffix="docs_new")
        vector_store.swap_collections(doc.kb_id, allow_empty=True)
        db.execute(delete(ChunkRecord).where(ChunkRecord.doc_id == doc.id))
        version = db.get(DocumentVersion, doc.published_version_id) if doc.published_version_id else None
        if version:
            version.review_status = "archived"
            append_review(db, version, actor_id, "archive", comment)
        doc.governance_status = "archived"
        doc.deleted_at = utcnow()
        doc.lock_version += 1
        record_in_transaction(db, db.get(User, actor_id), "document.archive", "document", doc.id,
                              detail={"kb_id": doc.kb_id, "comment": comment})
        db.commit()
        try:
            from app.core.keyword_index import keyword_index
            keyword_index.rebuild_kb(doc.kb_id)
        except Exception:
            logger.exception("归档后关键词索引重建失败：kb=%s", doc.kb_id)
    except Exception as exc:
        logger.exception("文档归档失败：doc=%s", doc_id)
        if doc is not None:
            vector_store.drop_temp_collection(doc.kb_id)
        db.rollback()
        doc = db.get(Document, doc_id)
        if doc:
            doc.error_message = f"归档失败：{exc}"
            db.commit()
    finally:
        db.close()
