"""回答反馈业务：所有权校验、幂等更新、状态流转和脱敏候选导出。"""
from __future__ import annotations

import json
import re
from uuid import uuid4
from sqlalchemy import func, select

from app.api.errors import ApiError, NotFoundError
from app.config import settings
from app.models.database import AnswerFeedback, ChatSession, Message, User, utcnow
from app.services.operations.audit import record_in_transaction

_PII = re.compile(r"1[3-9]\d{9}|\d{17}[\dXx]|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")


def require_answer_owner(db, message_id: int, user: User | None,
                         anonymous_id: str) -> tuple[Message, ChatSession, str]:
    message = db.get(Message, message_id)
    session = db.get(ChatSession, message.session_id) if message else None
    owned = bool(session and (
        (user is not None and session.user_id == user.id) or
        (user is None and session.user_id is None and session.anonymous_id == anonymous_id)
    ))
    if not message or message.role != "assistant" or not owned:
        raise NotFoundError("answer_not_found", "回答不存在")
    identity = f"user:{user.id}" if user else f"anon:{anonymous_id}"
    return message, session, identity


def upsert_feedback(db, message_id: int, user: User | None, anonymous_id: str,
                    rating: str, reason_code: str | None, comment: str | None) -> AnswerFeedback:
    message, session, identity = require_answer_owner(db, message_id, user, anonymous_id)
    if rating == "unhelpful" and not reason_code:
        raise ApiError(422, "feedback_reason_required", "认为回答没帮助时请选择原因")
    since = utcnow().replace(minute=0, second=0, microsecond=0)
    recent = db.scalar(select(func.count(AnswerFeedback.id)).where(
        AnswerFeedback.identity_key == identity, AnswerFeedback.updated_at >= since
    )) or 0
    existing = db.scalar(select(AnswerFeedback).where(
        AnswerFeedback.message_id == message_id, AnswerFeedback.identity_key == identity
    ))
    if existing is None and recent >= settings.feedback_rate_limit_per_hour:
        raise ApiError(429, "feedback_rate_limited", "反馈过于频繁，请稍后再试")
    clean_comment = (comment or "").strip() or None
    row = existing or AnswerFeedback(
        id=f"feedback_{uuid4().hex[:16]}", message_id=message_id,
        session_id=session.id, user_id=user.id if user else None,
        anonymous_id=None if user else anonymous_id, identity_key=identity,
        snapshot_json=json.dumps({"knowledge_scope": session.knowledge_base_id}, ensure_ascii=False),
    )
    row.rating, row.reason_code, row.comment = rating, reason_code, clean_comment
    row.status, row.updated_at = "pending", utcnow()
    if existing is None:
        db.add(row)
    record_in_transaction(db, user, "feedback.submit", "answer_feedback", row.id,
                          detail={"rating": rating, "reason_code": reason_code})
    db.commit(); db.refresh(row)
    return row


def sanitize_candidate(text: str) -> str:
    return _PII.sub("[已脱敏]", text).strip()[:1000]
