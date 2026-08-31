"""会话过期清理：超过保留期（默认 3 天）未更新的会话自动删除。

- 触发：启动时清理一次 + lifespan 定时任务（session_cleanup_interval_hours）
- 删除范围：sessions 行（级联 messages）+ 该会话贡献的长期记忆（memory collection）
- 幂等：按 updated_at 过滤，重复执行无副作用；失败仅记日志不中断服务
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.memory.long_term import long_term_memory
from app.models.database import ChatSession
from app.store.db import SessionLocal

logger = logging.getLogger(__name__)


def cleanup_expired_sessions() -> int:
    """删除超过保留期未更新的会话（含消息与长期记忆），返回删除数。"""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        days=settings.session_retention_days
    )
    db = SessionLocal()
    try:
        stale = db.scalars(select(ChatSession).where(ChatSession.updated_at < cutoff)).all()
        for s in stale:
            long_term_memory.clear_session(s.id, s.knowledge_base_id)  # 同步清记忆
            db.delete(s)  # 级联删除消息（cascade="all, delete-orphan"）
        db.commit()
        if stale:
            logger.info(
                "清理过期会话 %d 个（保留 %d 天，截止 %s）",
                len(stale), settings.session_retention_days, cutoff.date(),
            )
        return len(stale)
    except Exception:
        logger.exception("清理过期会话失败")
        return 0
    finally:
        db.close()
