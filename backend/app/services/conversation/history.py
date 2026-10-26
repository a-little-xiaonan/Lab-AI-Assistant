"""从已完成回合构建一次性历史快照。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.retrieval.retriever import estimate_tokens
from app.models.database import AnswerProvenance, ChatSession, ChatTurn, Message, utcnow
from app.observability.tracing import stage

RECENT_ANSWER_LIMIT = 8


@dataclass(frozen=True)
class HistoryTurn:
    turn_id: str
    user_message_id: int
    assistant_message_id: int
    user_content: str
    assistant_content: str


@dataclass(frozen=True)
class AnswerRef:
    """规划器可引用的历史回答候选；只保存稳定 ID 与依据类型。"""

    message_id: int
    turn_id: str
    answer_kind: str
    basis_status: str


@dataclass(frozen=True)
class HistorySnapshot:
    session_id: str
    revision: int
    turns: tuple[HistoryTurn, ...]
    summary: str | None
    text: str
    dropped_turns: int = 0
    recent_answer_refs: tuple[AnswerRef, ...] = ()
    pending_clarification: dict | None = None

    @property
    def message_ids(self) -> tuple[int, ...]:
        return tuple(
            message_id
            for turn in self.turns
            for message_id in (turn.user_message_id, turn.assistant_message_id)
        )

    @property
    def assistant_message_ids(self) -> tuple[int, ...]:
        return tuple(turn.assistant_message_id for turn in self.turns)

    @property
    def planner_text(self) -> str:
        """带消息 ID 的规划视图；正文只作为模型上下文，不写入诊断日志。"""
        parts = [f"[历史摘要] {self.summary}"] if self.summary else []
        for turn in self.turns:
            parts.extend((
                f"[user_message_id={turn.user_message_id}] user: {turn.user_content}",
                f"[assistant_message_id={turn.assistant_message_id}] assistant: {turn.assistant_content}",
            ))
        return "\n\n".join(parts)

    def candidate_text(self) -> str:
        """给规划器展示的候选回答清单，只能使用其中的 ID。"""
        lines = []
        by_id = {turn.assistant_message_id: turn for turn in self.turns}
        for ref in self.recent_answer_refs:
            turn = by_id.get(ref.message_id)
            if turn is None:
                continue
            excerpt = turn.assistant_content.replace("\n", " ")[:80]
            lines.append(
                f"- assistant_message_id={ref.message_id} 类型={ref.answer_kind} "
                f"依据={ref.basis_status} 摘要={excerpt}"
            )
        return "\n".join(lines)


def _format(summary: str | None, turns: list[HistoryTurn]) -> str:
    parts = [f"[历史摘要] {summary}"] if summary else []
    for turn in turns:
        parts.extend((f"user: {turn.user_content}", f"assistant: {turn.assistant_content}"))
    return "\n\n".join(parts)


def _load_answer_refs(db: Session, turns: list[HistoryTurn]) -> tuple[AnswerRef, ...]:
    """最近回答候选：没有依据记录的旧回合标记 unknown，不补造来源。"""
    selected = turns[-RECENT_ANSWER_LIMIT:]
    message_ids = [turn.assistant_message_id for turn in selected]
    rows = {
        row.assistant_message_id: row
        for row in db.scalars(
            select(AnswerProvenance).where(
                AnswerProvenance.assistant_message_id.in_(message_ids)
            )
        )
    } if message_ids else {}
    refs = []
    for turn in reversed(selected):
        provenance = rows.get(turn.assistant_message_id)
        refs.append(AnswerRef(
            message_id=turn.assistant_message_id,
            turn_id=turn.turn_id,
            answer_kind=provenance.answer_kind if provenance else "unknown",
            basis_status=provenance.basis_status if provenance else "unknown",
        ))
    return tuple(refs)


def _parse_pending_clarification(turn: ChatTurn | None) -> dict | None:
    if turn is None or turn.status != "completed" or turn.action != "clarify":
        return None
    if not turn.pending_clarification_json:
        return None
    try:
        state = json.loads(turn.pending_clarification_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(state, dict) or state.get("version") != "clarification-v1":
        return None
    expires_at = state.get("expires_at")
    try:
        if not isinstance(expires_at, str) or datetime.fromisoformat(expires_at) <= utcnow():
            return None
    except (TypeError, ValueError):
        return None
    if state.get("remaining_turns") is not None:
        try:
            if int(state["remaining_turns"]) < 1:
                return None
        except (TypeError, ValueError):
            return None
    state["_source_turn_id"] = turn.id
    return state


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
        latest_completed = completed[-1] if completed else None
        pending_clarification = _parse_pending_clarification(latest_completed)
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
        answer_refs = _load_answer_refs(db, turns)
        observed.set_attributes(
            history_turns=len(turns),
            history_dropped_turns=original_count - len(turns),
            history_chars=len(text),
            history_summary_present=bool(summary),
            answer_ref_count=len(answer_refs),
            pending_clarification=bool(pending_clarification),
        )
        return HistorySnapshot(
            session_id=session.id,
            revision=session.history_revision or 0,
            turns=tuple(turns),
            summary=summary,
            text=text,
            dropped_turns=original_count - len(turns),
            recent_answer_refs=answer_refs,
            pending_clarification=pending_clarification,
        )
