"""聊天 API：SSE 流式 + 非流式问答 + 会话 CRUD（对齐设计文档 §8）。

SSE 事件协议（前后端契约，Phase 2-01 定死）：
    event: meta    data: {"session_id": "sess_..."}            # 首帧
    event: delta   data: {"text": "<增量>"}                     # 多条
    event: done    data: {"full_text": "...", "sources": [...]}  # 终帧
    event: error   data: {"code": "...", "message": "..."}     # 替代 done，随后正常关闭

约定：
- session_id 由后端生成（sess_ 前缀）；未知 session_id 自动创建（行为固定并记录）
- user 消息在请求开始即落库；assistant 消息流结束后落库（避免中途写脏数据）
- 客户端断开 → 生成器提前结束，不写半条消息（CancelledError/GeneratorExit 不捕获）
- 短期记忆（Phase 2-03）：prompt 历史由 pipeline 经 memory_manager 产出；
  本层回合收尾将 user+assistant 写入内存窗口（回合原子：都成功才更新；
  流式 error/断连轮不更新，DB 里的孤儿 user 由 load_from_db 剔除规则兜底）
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import ApiError, BadRequestError, NotFoundError
from app.auth.dependencies import get_optional_current_user
from app.authorization.permissions import list_readable_kbs
from app.authorization.session_access import require_session_owner
from app.core.rag_pipeline import answer, answer_stream
from app.config import settings
from app.llm import qwen
from app.llm.errors import LLMError
from app.llm.prompt_templates import build_session_name_messages
from app.memory.long_term import long_term_memory
from app.memory.memory_manager import memory_manager
from app.models.database import ChatSession, Message, User, utcnow
from app.models.schemas import (
    ChatRequest,
    SessionBatchDelete,
    SessionCreate,
    SessionDetailOut,
    SessionOut,
    SessionRename,
)
from app.store.db import SessionLocal, get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])
ANONYMOUS_COOKIE = "rag_anonymous_id"
AUTO_KB_SCOPE = "kb_auto"


def _new_session_id() -> str:
    return f"sess_{uuid4().hex[:12]}"


def _session_matches(session: ChatSession, user: User | None, anonymous_id: str) -> bool:
    if user is not None:
        return session.user_id == user.id
    return session.user_id is None and session.anonymous_id == anonymous_id


def _get_or_create_session(
    db: Session,
    session_id: str | None,
    kb_id: str,
    user: User | None,
    anonymous_id: str,
) -> ChatSession:
    """带 session_id 只允许访问本人会话；不传才创建新会话。

    这替代了单用户时期“未知 id 自动创建、已删 id 自动复活”的行为，避免 ID 猜测越权。
    """
    if session_id:
        session = db.get(ChatSession, session_id)
        if session is not None and session.deleted_at is None and _session_matches(session, user, anonymous_id):
            return session
        raise NotFoundError("session_not_found", "会话不存在")
    session = ChatSession(
        id=_new_session_id(),
        knowledge_base_id=kb_id,
        user_id=user.id if user else None,
        anonymous_id=None if user else anonymous_id,
        name_source="ai",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _get_anonymous_id(request: Request) -> tuple[str, bool]:
    existing = request.cookies.get(ANONYMOUS_COOKIE)
    return (existing, False) if existing else (f"anon_{uuid4().hex[:16]}", True)


def _set_anonymous_cookie(response: Response, anonymous_id: str) -> None:
    response.set_cookie(
        ANONYMOUS_COOKIE,
        anonymous_id,
        max_age=7 * 24 * 3600,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/api",
    )


def format_sse_event(event: str, data: dict) -> str:
    """SSE 帧：data 单行 JSON（ensure_ascii=False，中文原样），帧以空行分隔。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _sse_stream(
    req: ChatRequest,
    request: Request,
    db: Session,
    session: ChatSession,
    user_id: str | None = None,
    readable_kb_ids: list[str] | None = None,
) -> None:
    """SSE 生成器：meta → delta* → done（或 error，替代 done 后正常关闭）。

    - user 消息先落库（请求开始时，spec）；assistant 消息在流结束后落库
    - 每块前后检查 is_disconnected，断连即停、不写 assistant（DB 无半条消息）
    - LLMError/ApiError/未知异常 → error 帧后正常结束（SSE 的 HTTP 状态码不可靠）
    - CancelledError/GeneratorExit（BaseException）不捕获，自然传播
    """
    db.add(Message(session_id=session.id, role="user", content=req.message))
    db.commit()
    yield format_sse_event("meta", {"session_id": session.id})

    sync_iter = answer_stream(
        req.message, readable_kb_ids or [], session_id=session.id, user_id=user_id
    )
    try:
        while True:
            if await request.is_disconnected():
                logger.info("客户端断开，终止流式回答：session=%s", session.id)
                return
            item = await asyncio.to_thread(next, sync_iter)
            if item["type"] == "delta":
                yield format_sse_event("delta", {"text": item["text"]})
            elif item["type"] == "done":
                # 先落库成功再发 done 帧：客户端收到 done 时数据已持久化
                db.add(
                    Message(
                        session_id=session.id, role="assistant", content=item["full_text"]
                    )
                )
                db.commit()
                _remember_turn(session.id, AUTO_KB_SCOPE, req.message, item["full_text"], user_id)
                yield format_sse_event(
                    "done",
                    {"full_text": item["full_text"], "sources": item["sources"]},
                )
                return
    except StopIteration:
        # answer_stream 未产出 done 即结束（理论不发生）：不落库 assistant
        logger.warning("流式回答意外结束（无 done 帧）：session=%s", session.id)
    except LLMError as exc:
        logger.error("流式回答失败：%s（%s）", exc.message, exc.code)
        yield format_sse_event("error", {"code": exc.code, "message": exc.message})
    except ApiError as exc:
        logger.error("流式回答业务错误：%s（%s）", exc.message, exc.code)
        yield format_sse_event("error", {"code": exc.code, "message": exc.message})
    except Exception as exc:
        logger.exception("流式回答未知异常：session=%s", session.id)
        yield format_sse_event("error", {"code": "internal_error", "message": "服务内部错误"})


def _remember_turn(
    session_id: str, kb_id: str, user_content: str, assistant_content: str, user_id: str | None
) -> None:
    """回合收尾：短期记忆窗口更新 + 长期记忆后台提取（都失败不影响主链路）。"""
    try:
        mem = memory_manager.get(session_id)
        mem.add_message("user", user_content)
        mem.add_message("assistant", assistant_content)
        memory_manager.persist_summary(session_id, mem.summary)
    except Exception:
        logger.exception("短期记忆更新失败：session=%s", session_id)
    if user_id is not None:  # 访客不建立永久个人画像
        _schedule_memory_extract(user_id, session_id, kb_id, user_content, assistant_content)
    _schedule_auto_name(session_id)  # 首轮后为未命名会话生成标题


def _schedule_background(fn) -> None:
    """后台线程执行（Fire-and-forget）：有事件循环走 to_thread，否则起守护线程。"""

    try:
        asyncio.get_running_loop().create_task(asyncio.to_thread(fn))
    except RuntimeError:  # 无事件循环的调用上下文兜底
        threading.Thread(target=fn, daemon=True).start()


def _schedule_memory_extract(
    user_id: str, session_id: str, kb_id: str, user_content: str, assistant_content: str
) -> None:
    """长期记忆提取放后台线程（Fire-and-forget：每轮多一次 LLM 调用，不阻塞回答）。"""

    def _run() -> None:
        long_term_memory.extract_and_store(
            user_id, session_id, kb_id, [("user", user_content), ("assistant", assistant_content)]
        )

    _schedule_background(_run)


def _auto_name_session(session_id: str) -> None:
    """AI 命名：首轮对话后为未命名会话生成标题（≤15 字）。

    用户已改名（name 非空）→ 跳过，不覆盖（用户优先）。
    """
    try:
        db = SessionLocal()
        try:
            session = db.get(ChatSession, session_id)
            if session is None or session.name or session.name_source == "user":
                return
            first = db.scalar(
                select(Message)
                .where(Message.session_id == session_id, Message.role == "user")
                .order_by(Message.id)
                .limit(1)
            )
            if first is None:
                return
            title = qwen.chat_completion(build_session_name_messages(first.content))
            title = title.strip().strip('"\'"').strip("「」").strip()[:20]
            if not title:
                return
            session.name = title
            session.name_source = "ai"
            db.commit()
            logger.info("AI 生成会话标题：%s → %s", session_id, title)
        finally:
            db.close()
    except Exception:
        logger.exception("会话命名失败：%s", session_id)


def _schedule_auto_name(session_id: str) -> None:
    _schedule_background(lambda: _auto_name_session(session_id))


@router.post("/chat")
async def chat(
    req: ChatRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_current_user),
) -> Response:
    if not req.message.strip():
        raise BadRequestError("empty_message", "消息不能为空")

    readable_kb_ids = [kb.id for kb in list_readable_kbs(db, user)]
    anonymous_id, set_anonymous = _get_anonymous_id(request)
    session = _get_or_create_session(db, req.session_id, AUTO_KB_SCOPE, user, anonymous_id)

    if req.stream:
        stream_response = StreamingResponse(
            _sse_stream(req, request, db, session, user.id if user else None, readable_kb_ids),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        if set_anonymous and user is None:
            _set_anonymous_cookie(stream_response, anonymous_id)
        return stream_response

    # 非流式（Phase 1 行为不变，历史由 pipeline 从短期记忆取）
    result = answer(req.message, readable_kb_ids, session_id=session.id, user_id=user.id if user else None)

    # 消息落库（user + assistant 成对）+ 记忆窗口更新
    db.add_all(
        [
            Message(session_id=session.id, role="user", content=req.message),
            Message(session_id=session.id, role="assistant", content=result["answer"]),
        ]
    )
    db.commit()
    _remember_turn(session.id, AUTO_KB_SCOPE, req.message, result["answer"], user.id if user else None)
    response = JSONResponse({"answer": result["answer"], "sources": result["sources"]})
    if set_anonymous and user is None:
        _set_anonymous_cookie(response, anonymous_id)
    return response


@router.post("/chat/sessions", response_model=SessionOut)
def create_session(
    request: Request,
    response: Response,
    body: SessionCreate | None = None,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_current_user),
) -> SessionOut:
    anonymous_id, set_anonymous = _get_anonymous_id(request)
    session = _get_or_create_session(db, None, AUTO_KB_SCOPE, user, anonymous_id)
    if body and body.name:
        session.name = body.name.strip()
        session.name_source = "user"
        db.commit()
        db.refresh(session)
    if set_anonymous and user is None:
        _set_anonymous_cookie(response, anonymous_id)
    return session


@router.put("/chat/sessions/{session_id}", response_model=SessionOut)
def rename_session(
    session_id: str,
    body: SessionRename,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_current_user),
) -> SessionOut:
    """用户改名（用户优先：AI 命名只在 name 为空时生成，不覆盖用户设置）。"""
    anonymous_id, _ = _get_anonymous_id(request)
    session = require_session_owner(db, session_id, user, include_deleted=False) if user else db.scalar(
        select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id.is_(None), ChatSession.anonymous_id == anonymous_id, ChatSession.deleted_at.is_(None))
    )
    if session is None:
        raise NotFoundError("session_not_found", "会话不存在")
    session.name = body.name.strip()
    session.name_source = "user"
    db.commit()
    db.refresh(session)
    return session


@router.get("/chat/sessions", response_model=list[SessionOut])
def list_sessions(
    request: Request, db: Session = Depends(get_db), user: User | None = Depends(get_optional_current_user)
) -> list[ChatSession]:
    anonymous_id, _ = _get_anonymous_id(request)
    owner_filter = ChatSession.user_id == user.id if user else (
        ChatSession.user_id.is_(None) & (ChatSession.anonymous_id == anonymous_id)
    )
    return list(
        db.scalars(
            select(ChatSession)
            .where(ChatSession.deleted_at.is_(None), owner_filter)
            .order_by(ChatSession.updated_at.desc())
        )
    )


@router.get("/chat/sessions/{session_id}", response_model=SessionDetailOut)
def get_session(
    session_id: str, request: Request, db: Session = Depends(get_db), user: User | None = Depends(get_optional_current_user)
) -> ChatSession:
    anonymous_id, _ = _get_anonymous_id(request)
    if user:
        return require_session_owner(db, session_id, user)
    session = db.scalar(select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id.is_(None), ChatSession.anonymous_id == anonymous_id, ChatSession.deleted_at.is_(None)))
    if session is None:
        raise NotFoundError("session_not_found", "会话不存在")
    return session


@router.delete("/chat/sessions/{session_id}")
def delete_session(
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_current_user),
) -> dict:
    """逻辑删除（软删）：标记 deleted_at，数据与记忆保留，可恢复（见 restore）。"""
    anonymous_id, _ = _get_anonymous_id(request)
    session = require_session_owner(db, session_id, user) if user else db.scalar(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.user_id.is_(None),
            ChatSession.anonymous_id == anonymous_id,
            ChatSession.deleted_at.is_(None),
        )
    )
    if session is None:
        raise NotFoundError("session_not_found", "会话不存在")
    session.deleted_at = utcnow()
    db.commit()
    return {"deleted": session_id}


@router.put("/chat/sessions/{session_id}/restore")
def restore_session(
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_current_user),
) -> SessionOut:
    """恢复已逻辑删除的会话（消息与长期记忆都保留）。"""
    anonymous_id, _ = _get_anonymous_id(request)
    if user:
        session = require_session_owner(db, session_id, user, include_deleted=True)
    else:
        session = db.scalar(select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.user_id.is_(None),
            ChatSession.anonymous_id == anonymous_id,
        ))
    if session is None:
        raise NotFoundError("session_not_found", "会话不存在")
    session.deleted_at = None
    db.commit()
    db.refresh(session)
    return session


@router.delete("/chat/sessions")
def batch_delete_sessions(
    body: SessionBatchDelete,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_current_user),
) -> dict:
    """批量逻辑删除（session_ids 指定；缺省或 all=true → 全部活跃会话）。"""
    anonymous_id, _ = _get_anonymous_id(request)
    owner_filter = ChatSession.user_id == user.id if user else (
        ChatSession.user_id.is_(None) & (ChatSession.anonymous_id == anonymous_id)
    )
    if body.all or not body.session_ids:
        targets = list(
            db.scalars(select(ChatSession).where(ChatSession.deleted_at.is_(None), owner_filter))
        )
    else:
        targets = [
            t
            for t in (
                db.scalar(
                    select(ChatSession).where(
                        ChatSession.id == sid, ChatSession.deleted_at.is_(None), owner_filter
                    )
                )
                for sid in body.session_ids
            )
            if t is not None
        ]
    for s in targets:
        s.deleted_at = utcnow()
    db.commit()
    logger.info("批量逻辑删除会话 %d 个（all=%s）", len(targets), body.all or not body.session_ids)
    return {"deleted": len(targets)}
