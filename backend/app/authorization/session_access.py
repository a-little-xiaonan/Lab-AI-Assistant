"""会话归属校验。登录用户绝不通过 session_id 猜测访问他人历史。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import NotFoundError
from app.models.database import ChatSession, User


def require_session_owner(
    db: Session, session_id: str, user: User | None, include_deleted: bool = False
) -> ChatSession:
    stmt = select(ChatSession).where(ChatSession.id == session_id)
    if not include_deleted:
        stmt = stmt.where(ChatSession.deleted_at.is_(None))
    session = db.scalar(stmt)
    # 对不属于当前用户的会话同样返回 404，避免泄露其是否存在。
    if session is None or session.user_id != (user.id if user else None):
        raise NotFoundError("session_not_found", "会话不存在")
    return session
