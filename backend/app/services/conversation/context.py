"""一次聊天回合内可安全传递的规划上下文；不携带共享数据库 Session。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from app.services.conversation.history import HistorySnapshot


@dataclass(frozen=True)
class ConversationContext:
    session_id: str
    snapshot: HistorySnapshot
    target_message_id: int | None
    db_factory: Callable[[], Session]
    readable_kb_ids: tuple[str, ...] = ()

    def open_db(self) -> Session:
        return self.db_factory()
