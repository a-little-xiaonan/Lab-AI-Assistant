"""聊天 API：非流式问答 + 会话 CRUD（对齐设计文档 §8）。

约定：
- session_id 由后端生成（sess_ 前缀）；未知 session_id 自动创建（行为固定并记录）
- stream=true 返回 400（Phase 2-01 实现 SSE）
- user 与 assistant 消息都落库（Phase 2-03 短期记忆、3-03 长期记忆的数据源）
- 历史拼入 prompt 由 pipeline 透传，本层不做截断（滑动窗口在 Phase 2-03）
"""
from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import BadRequestError, NotFoundError
from app.core.rag_pipeline import answer
from app.models.database import ChatSession, Message
from app.models.schemas import ChatRequest, ChatResponse, SessionCreate, SessionDetailOut, SessionOut
from app.store.db import get_db

router = APIRouter(tags=["chat"])


def _new_session_id() -> str:
    return f"sess_{uuid4().hex[:12]}"


def _get_or_create_session(db: Session, session_id: str | None, kb_id: str) -> ChatSession:
    if session_id:
        session = db.get(ChatSession, session_id)
        if session is not None:
            return session
        # 未知 session_id：自动创建（记录在案的一致行为）
        session = ChatSession(id=session_id, knowledge_base_id=kb_id)
    else:
        session = ChatSession(id=_new_session_id(), knowledge_base_id=kb_id)
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, db: Session = Depends(get_db)) -> ChatResponse:
    if req.stream:
        raise BadRequestError("stream_not_supported", "流式输出在 Phase 2 支持，请使用 stream=false")
    if not req.message.strip():
        raise BadRequestError("empty_message", "消息不能为空")

    session = _get_or_create_session(db, req.session_id, req.knowledge_base_id)
    history = [(m.role, m.content) for m in session.messages]

    result = answer(req.message, req.knowledge_base_id, session_id=session.id, history=history)

    # 消息落库（user + assistant 成对）
    db.add_all(
        [
            Message(session_id=session.id, role="user", content=req.message),
            Message(session_id=session.id, role="assistant", content=result["answer"]),
        ]
    )
    db.commit()
    return ChatResponse(answer=result["answer"], sources=result["sources"])


@router.post("/chat/sessions", response_model=SessionOut)
def create_session(body: SessionCreate | None = None, db: Session = Depends(get_db)) -> SessionOut:
    session = ChatSession(
        id=_new_session_id(),
        knowledge_base_id=body.knowledge_base_id if body else "kb_default",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@router.get("/chat/sessions", response_model=list[SessionOut])
def list_sessions(db: Session = Depends(get_db)) -> list[ChatSession]:
    return list(db.scalars(select(ChatSession).order_by(ChatSession.updated_at.desc())))


@router.get("/chat/sessions/{session_id}", response_model=SessionDetailOut)
def get_session(session_id: str, db: Session = Depends(get_db)) -> ChatSession:
    session = db.get(ChatSession, session_id)
    if session is None:
        raise NotFoundError("session_not_found", f"会话不存在：{session_id}")
    return session


@router.delete("/chat/sessions/{session_id}")
def delete_session(session_id: str, db: Session = Depends(get_db)) -> dict:
    session = db.get(ChatSession, session_id)
    if session is None:
        raise NotFoundError("session_not_found", f"会话不存在：{session_id}")
    db.delete(session)  # 级联删除消息（cascade="all, delete-orphan"）
    db.commit()
    return {"deleted": session_id}
