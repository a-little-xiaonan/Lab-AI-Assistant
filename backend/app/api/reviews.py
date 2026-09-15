"""文档审核发布 API：内容人员准备，管理员最终发布。"""
from __future__ import annotations

import json

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import NotFoundError
from app.auth.dependencies import require_roles
from app.authorization.policy import authorize, enforce_action, require_kb_permission
from app.core.document_publisher import archive_document, publish_document
from app.config import settings
from app.models.database import (
    Document, DocumentQualityCheck, DocumentReview, DocumentVersion, KnowledgeBase,
    DocumentVersionChunk, User,
)
from app.models.schemas import DocumentReviewAction, DocumentRollbackAction
from app.services.audit import record_in_transaction
from app.services.document_governance import current_version, require_document, transition
from app.store.db import get_db

router = APIRouter(tags=["document-reviews"])
require_editor = require_roles("editor", "admin")
require_admin = require_roles("admin")


def _summary(db: Session, doc: Document) -> dict:
    version = current_version(db, doc)
    return {
        "doc_id": doc.id, "kb_id": doc.kb_id, "filename": doc.filename,
        "governance_status": doc.governance_status,
        "version_id": version.id, "version_no": version.version_no,
        "review_status": version.review_status,
        "processing_status": version.processing_status,
        "chunk_count": version.chunk_count, "lock_version": doc.lock_version,
        "review_comment": doc.review_comment, "created_at": version.created_at,
    }


@router.get("/document-reviews")
def list_pending_reviews(db: Session = Depends(get_db), user: User = Depends(require_editor)) -> list[dict]:
    rows = db.scalars(select(Document).join(
        DocumentVersion, Document.current_version_id == DocumentVersion.id
    ).where(
        Document.deleted_at.is_(None),
        DocumentVersion.review_status.in_(["pending_review", "preapproved", "needs_change"]),
    ).order_by(Document.updated_at.desc())).all()
    return [_summary(db, doc) for doc in rows
            if authorize(user, "kb.manage", db.get(KnowledgeBase, doc.kb_id)).allowed]


@router.get("/knowledge-bases/{kb_id}/reviews")
def list_kb_reviews(kb_id: str, status: str = "pending_review",
                    db: Session = Depends(get_db), user: User = Depends(require_editor)) -> list[dict]:
    require_kb_permission(db, kb_id, user, "write")
    rows = db.scalars(select(Document).join(
        DocumentVersion, Document.current_version_id == DocumentVersion.id
    ).where(Document.kb_id == kb_id, Document.deleted_at.is_(None),
            DocumentVersion.review_status == status).order_by(Document.updated_at.desc())).all()
    return [_summary(db, doc) for doc in rows]


@router.get("/knowledge-bases/{kb_id}/documents/{doc_id}/versions")
def list_versions(kb_id: str, doc_id: str, db: Session = Depends(get_db),
                  user: User = Depends(require_editor)) -> list[dict]:
    require_kb_permission(db, kb_id, user, "write")
    doc = require_document(db, doc_id)
    if doc.kb_id != kb_id:
        raise NotFoundError("document_not_found", "文档不存在")
    rows = db.scalars(select(DocumentVersion).where(
        DocumentVersion.document_id == doc_id
    ).order_by(DocumentVersion.version_no.desc())).all()
    return [{"id": row.id, "version_no": row.version_no,
             "processing_status": row.processing_status, "review_status": row.review_status,
             "chunk_count": row.chunk_count, "file_size": row.file_size,
             "change_summary": row.change_summary, "created_by": row.created_by,
             "created_at": row.created_at} for row in rows]


@router.get("/knowledge-bases/{kb_id}/documents/{doc_id}/quality-checks")
def list_quality_checks(kb_id: str, doc_id: str, db: Session = Depends(get_db),
                        user: User = Depends(require_editor)) -> list[dict]:
    detail = review_detail(doc_id, db, user)
    if detail["kb_id"] != kb_id:
        raise NotFoundError("document_not_found", "文档不存在")
    return detail["quality_checks"]


@router.get("/knowledge-bases/{kb_id}/documents/{doc_id}/reviews")
def list_document_review_history(kb_id: str, doc_id: str, db: Session = Depends(get_db),
                                 user: User = Depends(require_editor)) -> list[dict]:
    detail = review_detail(doc_id, db, user)
    if detail["kb_id"] != kb_id:
        raise NotFoundError("document_not_found", "文档不存在")
    return detail["reviews"]


@router.get("/documents/{doc_id}/review-detail")
def review_detail(doc_id: str, db: Session = Depends(get_db),
                  user: User = Depends(require_editor)) -> dict:
    doc = require_document(db, doc_id)
    require_kb_permission(db, doc.kb_id, user, "write")
    version = current_version(db, doc)
    checks = db.scalars(select(DocumentQualityCheck).where(
        DocumentQualityCheck.document_version_id == version.id
    ).order_by(DocumentQualityCheck.created_at)).all()
    reviews = db.scalars(select(DocumentReview).where(
        DocumentReview.document_version_id == version.id
    ).order_by(DocumentReview.created_at)).all()
    chunks = db.scalars(select(DocumentVersionChunk).where(
        DocumentVersionChunk.document_version_id == version.id
    ).order_by(DocumentVersionChunk.chunk_index)).all()
    return {
        **_summary(db, doc),
        "quality_checks": [{
            "check_type": row.check_type, "severity": row.severity, "result": row.result,
            "details": json.loads(row.details_json or "{}"), "created_at": row.created_at,
        } for row in checks],
        "reviews": [{"action": row.action, "reviewer_id": row.reviewer_id,
                     "comment": row.comment, "created_at": row.created_at} for row in reviews],
        "chunks": [{"chunk_index": row.chunk_index, "text": row.text,
                    "char_length": row.char_length, "token_estimate": row.token_estimate,
                    "page": row.page, "slide_number": row.slide_number,
                    "sheet_name": row.sheet_name, "row_range": row.row_range} for row in chunks],
    }


def _do_action(doc_id: str, action: str, body: DocumentReviewAction,
               db: Session, user: User) -> dict:
    doc = require_document(db, doc_id)
    require_kb_permission(db, doc.kb_id, user, "write")
    version = current_version(db, doc)
    transition(db, doc, version, action, user.id, body.comment, body.expected_lock_version)
    record_in_transaction(db, user, f"document.{action}", "document", doc.id,
                          detail={"version_id": version.id, "comment": body.comment})
    db.commit()
    return _summary(db, doc)


@router.post("/documents/{doc_id}/submit-review")
def submit_review(doc_id: str, body: DocumentReviewAction, db: Session = Depends(get_db),
                  user: User = Depends(require_editor)) -> dict:
    return _do_action(doc_id, "submit", body, db, user)


@router.post("/documents/{doc_id}/preapprove")
def preapprove(doc_id: str, body: DocumentReviewAction, db: Session = Depends(get_db),
               user: User = Depends(require_editor)) -> dict:
    return _do_action(doc_id, "preapprove", body, db, user)


@router.post("/documents/{doc_id}/request-change")
def request_change(doc_id: str, body: DocumentReviewAction, db: Session = Depends(get_db),
                   user: User = Depends(require_editor)) -> dict:
    return _do_action(doc_id, "request_change", body, db, user)


@router.post("/documents/{doc_id}/reject")
def reject(doc_id: str, body: DocumentReviewAction, db: Session = Depends(get_db),
           user: User = Depends(require_admin)) -> dict:
    enforce_action(user, "document.publish")
    return _do_action(doc_id, "reject", body, db, user)


@router.post("/documents/{doc_id}/publish", status_code=202)
def publish(doc_id: str, body: DocumentReviewAction, background_tasks: BackgroundTasks,
            db: Session = Depends(get_db), user: User = Depends(require_admin)) -> dict:
    enforce_action(user, "document.publish")
    doc = require_document(db, doc_id)
    require_kb_permission(db, doc.kb_id, user, "write")
    from app.api.errors import ConflictError
    from app.services.document_governance import check_lock
    check_lock(doc, body.expected_lock_version)
    if settings.review_separation_enabled and doc.uploader_id == user.id:
        from app.api.errors import ApiError
        raise ApiError(403, "review_separation_required", "已开启职责分离，上传人不能发布自己的文档")
    version = current_version(db, doc)
    if version.review_status not in {"pending_review", "preapproved"}:
        raise ConflictError("invalid_review_status", "当前版本不处于可发布状态")
    version.review_status = "publishing"
    if doc.published_version_id is None:
        doc.governance_status = "publishing"
    record_in_transaction(db, user, "document.publish_requested", "document", doc.id,
                          detail={"version_id": version.id})
    db.commit()
    from app.services.job_queue import enqueue_job
    enqueue_job(
        "document_publish", "document", doc_id, user,
        {"doc_id": doc_id, "actor_id": user.id,
         "expected_lock_version": body.expected_lock_version, "comment": body.comment},
        f"document_publish:{doc_id}:{version.id}",
    )
    return {"doc_id": doc_id, "status": "publishing", "lock_version": doc.lock_version}


@router.post("/documents/{doc_id}/archive", status_code=202)
def archive(doc_id: str, body: DocumentReviewAction, background_tasks: BackgroundTasks,
            db: Session = Depends(get_db), user: User = Depends(require_admin)) -> dict:
    enforce_action(user, "document.archive")
    doc = require_document(db, doc_id)
    require_kb_permission(db, doc.kb_id, user, "write")
    background_tasks.add_task(archive_document, doc_id, user.id,
                              body.expected_lock_version, body.comment)
    return {"doc_id": doc_id, "status": "archiving", "lock_version": doc.lock_version}


# 知识库作用域兼容入口：路径中 kb_id 参与归属校验，避免伪造文档 ID 跨库操作。
def _require_doc_in_kb(db: Session, kb_id: str, doc_id: str) -> None:
    doc = require_document(db, doc_id)
    if doc.kb_id != kb_id:
        raise NotFoundError("document_not_found", "文档不存在")


@router.post("/knowledge-bases/{kb_id}/documents/{doc_id}/submit-review")
def submit_review_scoped(kb_id: str, doc_id: str, body: DocumentReviewAction,
                         db: Session = Depends(get_db), user: User = Depends(require_editor)) -> dict:
    _require_doc_in_kb(db, kb_id, doc_id)
    return submit_review(doc_id, body, db, user)


@router.post("/knowledge-bases/{kb_id}/documents/{doc_id}/preapprove")
def preapprove_scoped(kb_id: str, doc_id: str, body: DocumentReviewAction,
                      db: Session = Depends(get_db), user: User = Depends(require_editor)) -> dict:
    _require_doc_in_kb(db, kb_id, doc_id)
    return preapprove(doc_id, body, db, user)


@router.post("/knowledge-bases/{kb_id}/documents/{doc_id}/request-change")
def request_change_scoped(kb_id: str, doc_id: str, body: DocumentReviewAction,
                          db: Session = Depends(get_db), user: User = Depends(require_editor)) -> dict:
    _require_doc_in_kb(db, kb_id, doc_id)
    return request_change(doc_id, body, db, user)


@router.post("/knowledge-bases/{kb_id}/documents/{doc_id}/reject")
def reject_scoped(kb_id: str, doc_id: str, body: DocumentReviewAction,
                  db: Session = Depends(get_db), user: User = Depends(require_admin)) -> dict:
    _require_doc_in_kb(db, kb_id, doc_id)
    return reject(doc_id, body, db, user)


@router.post("/knowledge-bases/{kb_id}/documents/{doc_id}/publish", status_code=202)
def publish_scoped(kb_id: str, doc_id: str, body: DocumentReviewAction,
                   background_tasks: BackgroundTasks, db: Session = Depends(get_db),
                   user: User = Depends(require_admin)) -> dict:
    _require_doc_in_kb(db, kb_id, doc_id)
    return publish(doc_id, body, background_tasks, db, user)


@router.post("/knowledge-bases/{kb_id}/documents/{doc_id}/archive", status_code=202)
def archive_scoped(kb_id: str, doc_id: str, body: DocumentReviewAction,
                   background_tasks: BackgroundTasks, db: Session = Depends(get_db),
                   user: User = Depends(require_admin)) -> dict:
    _require_doc_in_kb(db, kb_id, doc_id)
    return archive(doc_id, body, background_tasks, db, user)


@router.post("/knowledge-bases/{kb_id}/documents/{doc_id}/rollback", status_code=202)
def rollback_scoped(kb_id: str, doc_id: str, body: DocumentRollbackAction,
                    background_tasks: BackgroundTasks, db: Session = Depends(get_db),
                    user: User = Depends(require_admin)) -> dict:
    """把历史版本设为发布候选，再复用同一双缓冲发布流程。"""
    enforce_action(user, "document.rollback")
    _require_doc_in_kb(db, kb_id, doc_id)
    doc = require_document(db, doc_id)
    if doc.lock_version != body.expected_lock_version:
        from app.api.errors import ConflictError
        raise ConflictError("document_version_conflict", "文档已变化，请刷新后重试")
    version = db.get(DocumentVersion, body.version_id)
    if version is None or version.document_id != doc.id:
        raise NotFoundError("document_version_not_found", "历史版本不存在")
    staged = db.scalar(select(DocumentVersionChunk.id).where(
        DocumentVersionChunk.document_version_id == version.id
    ).limit(1))
    if staged is None:
        from app.api.errors import ConflictError
        raise ConflictError("version_chunks_unavailable", "该历史版本没有保留待审分块，无法自动回滚")
    doc.current_version_id = version.id
    doc.current_version = version.version_no
    version.review_status = "preapproved"
    doc.lock_version += 1
    from app.services.document_governance import append_review
    append_review(db, version, user.id, "rollback", body.comment)
    db.commit()
    background_tasks.add_task(publish_document, doc.id, user.id, doc.lock_version, body.comment)
    return {"doc_id": doc.id, "status": "publishing", "version_id": version.id,
            "lock_version": doc.lock_version}
