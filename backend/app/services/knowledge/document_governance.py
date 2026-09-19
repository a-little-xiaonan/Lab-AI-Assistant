"""文档审核状态机：版本不可变、审核流水只追加、写操作使用乐观锁。"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import ConflictError, NotFoundError
from app.models.database import Document, DocumentQualityCheck, DocumentReview, DocumentVersion, utcnow


def require_document(db: Session, doc_id: str) -> Document:
    doc = db.get(Document, doc_id)
    if doc is None or doc.deleted_at is not None:
        raise NotFoundError("document_not_found", "文档不存在或已归档")
    return doc


def current_version(db: Session, doc: Document) -> DocumentVersion:
    version = db.get(DocumentVersion, doc.current_version_id) if doc.current_version_id else None
    if version is None:
        raise NotFoundError("document_version_not_found", "当前文档版本不存在")
    return version


def check_lock(doc: Document, expected: int) -> None:
    if doc.lock_version != expected:
        raise ConflictError("document_version_conflict", "文档已被其他人修改，请刷新后重试")


def append_review(db: Session, version: DocumentVersion, reviewer_id: str | None,
                  action: str, comment: str | None = None) -> DocumentReview:
    row = DocumentReview(
        id=f"review_{uuid4().hex[:20]}", document_version_id=version.id,
        reviewer_id=reviewer_id, action=action, comment=comment,
    )
    db.add(row)
    return row


def has_blocking_failure(db: Session, version_id: str) -> bool:
    return db.scalar(select(DocumentQualityCheck.id).where(
        DocumentQualityCheck.document_version_id == version_id,
        DocumentQualityCheck.severity == "blocking",
        DocumentQualityCheck.result == "failed",
    ).limit(1)) is not None


def transition(db: Session, doc: Document, version: DocumentVersion, action: str,
               reviewer_id: str, comment: str | None, expected_lock: int) -> None:
    check_lock(doc, expected_lock)
    if action == "submit":
        if version.processing_status != "ready" or has_blocking_failure(db, version.id):
            raise ConflictError("quality_check_failed", "文档尚未处理完成或存在阻断性质量问题")
        version.review_status = "pending_review"
        if doc.published_version_id is None:
            doc.governance_status = "pending_review"
    elif action == "preapprove":
        version.review_status = "preapproved"
        doc.reviewed_by, doc.reviewed_at = reviewer_id, utcnow()
    elif action in {"reject", "request_change"}:
        version.review_status = "rejected" if action == "reject" else "needs_change"
        if doc.published_version_id is None:
            doc.governance_status = version.review_status
        doc.review_comment = comment
    else:
        raise ValueError(f"未知审核动作：{action}")
    doc.lock_version += 1
    doc.updated_at = utcnow()
    append_review(db, version, reviewer_id, action, comment)
