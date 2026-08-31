"""长期记忆清理 API（Phase 3-03）：清某会话贡献的记忆。

隐私底线：记忆存用户个人信息（偏好/实体），必须提供清除入口。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.errors import NotFoundError
from app.memory.long_term import long_term_memory
from app.models.database import ChatSession
from app.store.db import get_db

router = APIRouter(tags=["memory"])


@router.delete("/memory/{session_id}")
def clear_session_memory(session_id: str, db: Session = Depends(get_db)) -> dict:
    """删除某会话贡献的全部长期记忆，返回删除条数。"""
    session = db.get(ChatSession, session_id)
    if session is None:
        raise NotFoundError("session_not_found", f"会话不存在：{session_id}")
    deleted = long_term_memory.clear_session(session_id, session.knowledge_base_id)
    return {"deleted": deleted}
