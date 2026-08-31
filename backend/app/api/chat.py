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
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import ApiError, BadRequestError, NotFoundError
from app.core.rag_pipeline import answer, answer_stream
from app.llm import qwen
from app.llm.errors import LLMError
from app.llm.prompt_templates import build_session_name_messages
from app.memory.long_term import long_term_memory
from app.memory.memory_manager import memory_manager
from app.models.database import ChatSession, Message
from app.models.schemas import (
    ChatRequest,
    SessionCreate,
    SessionDetailOut,
    SessionOut,
    SessionRename,
)
from app.store.db import SessionLocal, get_db

logger = logging.getLogger(__name__)

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


def format_sse_event(event: str, data: dict) -> str:
    """SSE 帧：data 单行 JSON（ensure_ascii=False，中文原样），帧以空行分隔。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _sse_stream(
    req: ChatRequest, request: Request, db: Session, session: ChatSession
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

    sync_iter = answer_stream(req.message, req.knowledge_base_id, session_id=session.id)
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
                _remember_turn(session.id, req.knowledge_base_id, req.message, item["full_text"])
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
    session_id: str, kb_id: str, user_content: str, assistant_content: str
) -> None:
    """回合收尾：短期记忆窗口更新 + 长期记忆后台提取（都失败不影响主链路）。"""
    try:
        mem = memory_manager.get(session_id)
        mem.add_message("user", user_content)
        mem.add_message("assistant", assistant_content)
    except Exception:
        logger.exception("短期记忆更新失败：session=%s", session_id)
    _schedule_memory_extract(session_id, kb_id, user_content, assistant_content)
    _schedule_auto_name(session_id)  # 首轮后为未命名会话生成标题


def _schedule_background(fn) -> None:
    """后台线程执行（Fire-and-forget）：有事件循环走 to_thread，否则起守护线程。"""

    try:
        asyncio.get_running_loop().create_task(asyncio.to_thread(fn))
    except RuntimeError:  # 无事件循环的调用上下文兜底
        threading.Thread(target=fn, daemon=True).start()


def _schedule_memory_extract(
    session_id: str, kb_id: str, user_content: str, assistant_content: str
) -> None:
    """长期记忆提取放后台线程（Fire-and-forget：每轮多一次 LLM 调用，不阻塞回答）。"""

    def _run() -> None:
        long_term_memory.extract_and_store(
            session_id, kb_id, [("user", user_content), ("assistant", assistant_content)]
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
            if session is None or session.name:
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
) -> Response:
    if not req.message.strip():
        raise BadRequestError("empty_message", "消息不能为空")

    session = _get_or_create_session(db, req.session_id, req.knowledge_base_id)

    if req.stream:
        return StreamingResponse(
            _sse_stream(req, request, db, session),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式（Phase 1 行为不变，历史由 pipeline 从短期记忆取）
    result = answer(req.message, req.knowledge_base_id, session_id=session.id)

    # 消息落库（user + assistant 成对）+ 记忆窗口更新
    db.add_all(
        [
            Message(session_id=session.id, role="user", content=req.message),
            Message(session_id=session.id, role="assistant", content=result["answer"]),
        ]
    )
    db.commit()
    _remember_turn(session.id, req.knowledge_base_id, req.message, result["answer"])
    return {"answer": result["answer"], "sources": result["sources"]}


@router.post("/chat/sessions", response_model=SessionOut)
def create_session(body: SessionCreate | None = None, db: Session = Depends(get_db)) -> SessionOut:
    session = ChatSession(
        id=_new_session_id(),
        knowledge_base_id=body.knowledge_base_id if body else "kb_default",
        name=body.name if body else None,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@router.put("/chat/sessions/{session_id}", response_model=SessionOut)
def rename_session(
    session_id: str, body: SessionRename, db: Session = Depends(get_db)
) -> SessionOut:
    """用户改名（用户优先：AI 命名只在 name 为空时生成，不覆盖用户设置）。"""
    session = db.get(ChatSession, session_id)
    if session is None:
        raise NotFoundError("session_not_found", f"会话不存在：{session_id}")
    session.name = body.name.strip()
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
