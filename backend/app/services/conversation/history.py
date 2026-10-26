"""从已完成回合构建一次性历史快照。"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.retrieval.retriever import estimate_tokens
from app.models.database import ChatSession, ChatTurn, Message
from app.observability.tracing import stage


@dataclass(frozen=True)
class HistoryTurn:
    turn_id: str
    user_message_id: int
    assistant_message_id: int
    user_content: str
    assistant_content: str


@dataclass(frozen=True)
class HistorySnapshot:
    session_id: str
    revision: int
    turns: tuple[HistoryTurn, ...]
    summary: str | None
    text: str
    dropped_turns: int = 0

    @property
    def message_ids(self) -> tuple[int, ...]:
        return tuple(
            message_id
            for turn in self.turns
            for message_id in (turn.user_message_id, turn.assistant_message_id)
        )


def _format(summary: str | None, turns: list[HistoryTurn]) -> str:
    parts = [f"[历史摘要] {summary}"] if summary else []
    for turn in turns:
        parts.extend((f"user: {turn.user_content}", f"assistant: {turn.assistant_content}"))
    return "\n\n".join(parts)


def build_history_snapshot(db: Session, session: ChatSession) -> HistorySnapshot:
    """只读取 completed 回合，并始终按完整回合裁剪。"""
    with stage("history.snapshot", history_revision=session.history_revision or 0) as observed:
        completed = list(db.scalars(
            select(ChatTurn)
            .where(ChatTurn.session_id == session.id, ChatTurn.status == "completed")
            .order_by(ChatTurn.finished_at.desc(), ChatTurn.id.desc())
            .limit(max(settings.history_max_turns * 2, settings.history_max_turns))
        ))
        completed.reverse()
        summary = None
        if session.summary and session.summary_through_turn_id:
            boundary = db.get(ChatTurn, session.summary_through_turn_id)
            if (boundary is not None and boundary.session_id == session.id
                    and boundary.status == "completed"):
                summary = session.summary
                ids = [turn.id for turn in completed]
                if boundary.id in ids:
                    completed = completed[ids.index(boundary.id) + 1:]
                elif boundary.finished_at is not None:
                    completed = [
                        turn for turn in completed
                        if turn.finished_at is not None and turn.finished_at > boundary.finished_at
                    ]
        turn_ids = [turn.id for turn in completed]
        rows = list(db.scalars(
            select(Message)
            .where(Message.turn_id.in_(turn_ids))
            .order_by(Message.id)
        )) if turn_ids else []
        by_turn: dict[str, dict[str, Message]] = {}
        for message in rows:
            by_turn.setdefault(message.turn_id or "", {})[message.role] = message

        turns = []
        for turn in completed:
            pair = by_turn.get(turn.id, {})
            user_message, assistant_message = pair.get("user"), pair.get("assistant")
            if user_message is None or assistant_message is None:
                continue
            turns.append(HistoryTurn(
                turn_id=turn.id,
                user_message_id=user_message.id,
                assistant_message_id=assistant_message.id,
                user_content=user_message.content,
                assistant_content=assistant_message.content,
            ))

        original_count = len(turns)
        if summary and estimate_tokens(summary) > settings.history_max_tokens // 2:
            summary = None
        text = _format(summary, turns)
        while turns and estimate_tokens(text) > settings.history_max_tokens:
            turns.pop(0)
            text = _format(summary, turns)
        observed.set_attributes(
            history_turns=len(turns),
            history_dropped_turns=original_count - len(turns),
            history_chars=len(text),
            history_summary_present=bool(summary),
        )
        return HistorySnapshot(
            session_id=session.id,
            revision=session.history_revision or 0,
            turns=tuple(turns),
            summary=summary,
            text=text,
            dropped_turns=original_count - len(turns),
        )
