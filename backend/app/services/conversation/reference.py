"""统一目标消息解析：显式引用、模型候选和待澄清目标都走同一套校验。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.database import ChatSession, Message
from app.observability.tracing import stage
from app.services.conversation.plan import ConversationPlan
from app.services.conversation.history import HistorySnapshot


class TargetResolutionError(ValueError):
    """目标缺失、越权或已失效；调用方应返回澄清而不是猜测。"""


def _load_assistant(db: Session, session: ChatSession, message_id: int) -> Message:
    message = db.get(Message, message_id)
    if (message is None or message.session_id != session.id
            or message.role != "assistant"):
        raise TargetResolutionError("目标回答不属于当前会话")
    if session.deleted_at is not None:
        raise TargetResolutionError("会话已删除")
    return message


def resolve_explicit_target(
    db: Session, session: ChatSession, message_id: int | None
) -> Message | None:
    if message_id is None:
        return None
    return _load_assistant(db, session, message_id)


def resolve_targets(
    db: Session,
    session: ChatSession,
    plan: ConversationPlan,
    *,
    explicit_target: Message | None,
    candidate_ids: set[int],
    pending_clarification: dict | None = None,
) -> tuple[Message, ...]:
    """按优先级解析目标；模型只能选择服务端候选集合中的 ID。"""
    with stage(
        "reference.resolve",
        action=plan.action,
        plan_version=plan.planner_version,
        reference_message_ids=list(plan.reference_message_ids),
        target_count=len(plan.reference_message_ids),
    ) as observed:
        targets: list[Message] = []
        seen: set[int] = set()

        def add(message: Message) -> None:
            if message.id not in seen:
                seen.add(message.id)
                targets.append(message)

        if explicit_target is not None:
            add(explicit_target)
        for message_id in plan.reference_message_ids:
            if message_id not in candidate_ids and (
                explicit_target is None or message_id != explicit_target.id
            ):
                raise TargetResolutionError("目标回答不在服务端候选集合中")
            add(_load_assistant(db, session, message_id))
        if (
            not targets
            and pending_clarification
            and pending_clarification.get("reference_message_ids")
        ):
            for message_id in pending_clarification["reference_message_ids"][:3]:
                if isinstance(message_id, int) and message_id in candidate_ids:
                    add(_load_assistant(db, session, message_id))
        observed.set_attributes(target_message_ids=[message.id for message in targets],
                                target_count=len(targets))
        return tuple(targets)


def target_context(
    db: Session,
    session: ChatSession,
    targets: tuple[Message, ...],
    *,
    max_chars: int | None = None,
) -> str:
    """按已验证 ID 单独加载目标正文；旧候选正文不进入常规历史预算。"""
    if not targets:
        return ""
    limit = max_chars or settings.conversation_target_max_chars
    sections = []
    used = 0
    for message in targets:
        # 重新按 ID 读取，避免调用方传入过期对象或跨会话内容。
        current = db.get(Message, message.id)
        if (current is None or current.session_id != session.id
                or current.role != "assistant"):
            continue
        text = current.content
        remaining = limit - used
        if remaining <= 0:
            break
        excerpt = text[:remaining]
        sections.append(f"[历史回答 message_id={current.id}]\n{excerpt}")
        used += len(excerpt)
    return "\n\n".join(sections)


def candidate_ids(snapshot: HistorySnapshot) -> set[int]:
    return {ref.message_id for ref in snapshot.recent_answer_refs}


def recent_target_for_clarification(snapshot: HistorySnapshot) -> int | None:
    """没有显式目标时，只有最近一条回答可作为安全默认候选。"""
    return snapshot.recent_answer_refs[0].message_id if snapshot.recent_answer_refs else None


def load_session_message(db: Session, session_id: str, message_id: int) -> Message | None:
    return db.scalar(
        select(Message).where(
            Message.id == message_id,
            Message.session_id == session_id,
            Message.role == "assistant",
        )
    )
