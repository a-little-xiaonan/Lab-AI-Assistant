"""记忆管理器（Phase 2-03）：进程内实例注册表 + DB 重建。

- 进程内 dict：{session_id: (ShortTermMemory, last_access_ts)}，threading.Lock 保护
- get() 未命中 → load_from_db 重建后缓存；命中 → 刷新访问时间
- 容量/过期清理：memory_max_instances 超限 LRU 淘汰、memory_idle_ttl_seconds
  空闲过期。**淘汰安全**：DB 是持久源，下次访问重建即可（良性竞态：流式中途被
  淘汰仅丢缓存，重建结果一致）
- load_from_db：剔除末尾"无回应的 user 消息"（当前轮已落库但未完成，或错误孤儿），
  避免当前问题重复进入上文（流式路径 user 先落库的时序坑）
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import select

from app.config import settings
from app.memory.short_term import ShortTermMemory
from app.models.database import Message
from app.store.db import SessionLocal

logger = logging.getLogger(__name__)


class MemoryManager:
    def __init__(
        self,
        max_instances: int | None = None,
        idle_ttl_seconds: int | None = None,
    ) -> None:
        self._max_instances = max_instances or settings.memory_max_instances
        self._idle_ttl_seconds = idle_ttl_seconds or settings.memory_idle_ttl_seconds
        self._memories: dict[str, tuple[ShortTermMemory, float]] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> ShortTermMemory:
        """取会话记忆实例：未缓存则从 DB 重建；命中刷新访问时间。"""
        now = time.monotonic()
        with self._lock:
            entry = self._memories.get(session_id)
            if entry is not None:
                mem, _ = entry
                self._memories[session_id] = (mem, now)
                return mem
        mem = self.load_from_db(session_id)
        with self._lock:
            self._evict(now)
            self._memories[session_id] = (mem, now)
        return mem

    def _evict(self, now: float) -> None:
        """过期淘汰 + 超容量 LRU（最久未访问先淘汰，至少保留最近一个）。"""
        expired = [sid for sid, (_, ts) in self._memories.items() if now - ts > self._idle_ttl_seconds]
        for sid in expired:
            del self._memories[sid]
        if len(self._memories) >= self._max_instances:
            for sid, (_, ts) in sorted(self._memories.items(), key=lambda kv: kv[1][1])[
                : len(self._memories) - self._max_instances + 1
            ]:
                del self._memories[sid]

    def load_from_db(self, session_id: str) -> ShortTermMemory:
        """从 messages 表重建窗口（会话重启不丢）；自建会话，不依赖调用方。"""
        db = SessionLocal()
        try:
            rows = db.scalars(
                select(Message)
                .where(Message.session_id == session_id)
                .order_by(Message.id)  # id 递增即时间序
            ).all()
            pairs = [(m.role, m.content) for m in rows]
        finally:
            db.close()
        # 剔除末尾无回应的 user（当前轮或错误孤儿，均不该进上文）
        while pairs and pairs[-1][0] == "user":
            pairs.pop()
        mem = ShortTermMemory(session_id, max_turns=settings.history_max_turns)
        mem._rebuild(pairs)
        return mem


memory_manager = MemoryManager()  # 模块级单例
