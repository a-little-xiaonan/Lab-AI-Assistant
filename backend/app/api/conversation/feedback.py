"""问答运营：招新推荐问题、回答反馈和管理员反馈分诊。"""
from __future__ import annotations

import json
from uuid import uuid4
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.errors import NotFoundError
from app.auth.dependencies import get_optional_current_user, require_roles
from app.authorization.policy import LEVEL_VALUE, effective_level
from app.models.database import AnswerFeedback, Message, SuggestedQuestion, User, utcnow
from app.models.schemas import FeedbackCreate, FeedbackReview, SuggestedQuestionCreate, SuggestedQuestionUpdate
from app.services.operations.audit import record_in_transaction
from app.services.operations.feedback import require_answer_owner, sanitize_candidate, upsert_feedback
from app.store.db import get_db

router = APIRouter(tags=["feedback"])


def _anonymous_id(request: Request) -> str:
    return request.cookies.get("rag_anonymous_id", "")


def _feedback_out(row: AnswerFeedback) -> dict:
    return {"id": row.id, "message_id": row.message_id, "session_id": row.session_id,
            "user_id": row.user_id, "rating": row.rating, "reason_code": row.reason_code,
            "comment": row.comment, "status": row.status, "resolution_note": row.resolution_note,
            "evaluation_candidate": row.evaluation_candidate,
            "created_at": row.created_at, "updated_at": row.updated_at,
            "resolved_at": row.resolved_at}


@router.get("/chat/suggested-questions")
def suggested_questions(user: User | None = Depends(get_optional_current_user),
                        db: Session = Depends(get_db)) -> list[dict]:
    level = LEVEL_VALUE[effective_level(user)]
    rows = db.scalars(select(SuggestedQuestion).where(
        SuggestedQuestion.enabled.is_(True)
    ).order_by(SuggestedQuestion.sort_order, SuggestedQuestion.category)).all()
    return [{"id": row.id, "category": row.category, "question": row.question}
            for row in rows if LEVEL_VALUE[row.required_level] <= level]


@router.post("/chat/messages/{message_id}/feedback")
def save_feedback(message_id: int, body: FeedbackCreate, request: Request,
                  user: User | None = Depends(get_optional_current_user),
                  db: Session = Depends(get_db)) -> dict:
    return _feedback_out(upsert_feedback(db, message_id, user, _anonymous_id(request),
                                        body.rating, body.reason_code, body.comment))


@router.get("/chat/messages/{message_id}/feedback")
def get_feedback(message_id: int, request: Request,
                 user: User | None = Depends(get_optional_current_user),
                 db: Session = Depends(get_db)) -> dict | None:
    _, _, identity = require_answer_owner(db, message_id, user, _anonymous_id(request))
    row = db.scalar(select(AnswerFeedback).where(
        AnswerFeedback.message_id == message_id, AnswerFeedback.identity_key == identity
    ))
    return _feedback_out(row) if row else None


@router.delete("/chat/messages/{message_id}/feedback", status_code=204)
def delete_feedback(message_id: int, request: Request,
                    user: User | None = Depends(get_optional_current_user),
                    db: Session = Depends(get_db)) -> None:
    _, _, identity = require_answer_owner(db, message_id, user, _anonymous_id(request))
    row = db.scalar(select(AnswerFeedback).where(
        AnswerFeedback.message_id == message_id, AnswerFeedback.identity_key == identity
    ))
    if row:
        db.delete(row); db.commit()


@router.get("/admin/feedback")
def admin_feedback(status: str | None = None, reason_code: str | None = None,
                   offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                   _operator: User = Depends(require_roles("editor", "admin")),
                   db: Session = Depends(get_db)) -> dict:
    conditions = []
    if status: conditions.append(AnswerFeedback.status == status)
    if reason_code: conditions.append(AnswerFeedback.reason_code == reason_code)
    total = db.scalar(select(func.count(AnswerFeedback.id)).where(*conditions)) or 0
    rows = db.scalars(select(AnswerFeedback).where(*conditions).order_by(
        AnswerFeedback.created_at.desc()).offset(offset).limit(limit)).all()
    return {"total": total, "items": [_feedback_out(row) for row in rows]}


def _transition(db: Session, feedback_id: str, status: str, body: FeedbackReview,
                actor: User) -> dict:
    row = db.get(AnswerFeedback, feedback_id)
    if row is None: raise NotFoundError("feedback_not_found", "反馈不存在")
    row.status, row.reviewer_id = status, actor.id
    row.resolution_note = body.resolution_note.strip()
    row.resolved_at = utcnow() if status in {"resolved", "ignored"} else None
    record_in_transaction(db, actor, f"feedback.{status}", "answer_feedback", row.id)
    db.commit(); return _feedback_out(row)


@router.post("/admin/feedback/{feedback_id}/triage")
def triage(feedback_id: str, body: FeedbackReview, actor: User = Depends(require_roles("editor", "admin")), db: Session = Depends(get_db)) -> dict:
    return _transition(db, feedback_id, "triaged", body, actor)


@router.post("/admin/feedback/{feedback_id}/resolve")
def resolve(feedback_id: str, body: FeedbackReview, actor: User = Depends(require_roles("editor", "admin")), db: Session = Depends(get_db)) -> dict:
    return _transition(db, feedback_id, "resolved", body, actor)


@router.post("/admin/feedback/{feedback_id}/ignore")
def ignore(feedback_id: str, body: FeedbackReview, actor: User = Depends(require_roles("editor", "admin")), db: Session = Depends(get_db)) -> dict:
    return _transition(db, feedback_id, "ignored", body, actor)


@router.post("/admin/feedback/{feedback_id}/evaluation-candidate")
def evaluation_candidate(feedback_id: str, actor: User = Depends(require_roles("editor", "admin")), db: Session = Depends(get_db)) -> dict:
    row = db.get(AnswerFeedback, feedback_id)
    if row is None: raise NotFoundError("feedback_not_found", "反馈不存在")
    message = db.get(Message, row.message_id)
    question = db.scalar(select(Message).where(Message.session_id == row.session_id,
        Message.role == "user", Message.id < row.message_id).order_by(Message.id.desc()).limit(1))
    snapshot = json.loads(row.snapshot_json or "{}")
    snapshot["candidate_question"] = sanitize_candidate(question.content if question else "")
    snapshot["candidate_comment"] = sanitize_candidate(row.comment or "")
    row.snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    row.evaluation_candidate = True
    record_in_transaction(db, actor, "feedback.evaluation_candidate", "answer_feedback", row.id)
    db.commit(); return _feedback_out(row)


def _question_out(row: SuggestedQuestion) -> dict:
    return {"id": row.id, "category": row.category, "question": row.question,
            "required_level": row.required_level, "sort_order": row.sort_order,
            "enabled": row.enabled, "source_type": row.source_type,
            "created_at": row.created_at, "updated_at": row.updated_at}


@router.get("/admin/suggested-questions")
def admin_questions(_actor: User = Depends(require_roles("editor", "admin")), db: Session = Depends(get_db)) -> list[dict]:
    return [_question_out(row) for row in db.scalars(select(SuggestedQuestion).order_by(SuggestedQuestion.sort_order))]


@router.post("/admin/suggested-questions", status_code=201)
def create_question(body: SuggestedQuestionCreate, actor: User = Depends(require_roles("editor", "admin")), db: Session = Depends(get_db)) -> dict:
    row = SuggestedQuestion(id=f"sq_{uuid4().hex[:16]}", created_by=actor.id, source_type="manual", **body.model_dump())
    db.add(row); record_in_transaction(db, actor, "suggested_question.create", "suggested_question", row.id); db.commit(); return _question_out(row)


@router.put("/admin/suggested-questions/{question_id}")
def update_question(question_id: str, body: SuggestedQuestionUpdate, actor: User = Depends(require_roles("editor", "admin")), db: Session = Depends(get_db)) -> dict:
    row = db.get(SuggestedQuestion, question_id)
    if row is None: raise NotFoundError("suggested_question_not_found", "推荐问题不存在")
    for key, value in body.model_dump().items(): setattr(row, key, value)
    record_in_transaction(db, actor, "suggested_question.update", "suggested_question", row.id); db.commit(); return _question_out(row)


@router.delete("/admin/suggested-questions/{question_id}", status_code=204)
def delete_question(question_id: str, actor: User = Depends(require_roles("editor", "admin")), db: Session = Depends(get_db)) -> None:
    row = db.get(SuggestedQuestion, question_id)
    if row is None: raise NotFoundError("suggested_question_not_found", "推荐问题不存在")
    record_in_transaction(db, actor, "suggested_question.delete", "suggested_question", row.id); db.delete(row); db.commit()
