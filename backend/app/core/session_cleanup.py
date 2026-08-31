"""会话过期清理（两层）：逻辑删除 + 物理清理。

1. **活跃会话超保留期**（session_retention_days，默认 3 天）未更新 → 逻辑删除
   （deleted_at 标记，数据与记忆保留，可恢复）
2. **已逻辑删除超清理期**（session_purge_days，默认 30 天）→ 物理删除
   （级联消息 + 同步清该会话的长期记忆，防软删数据无限膨胀）

- 触发：启动时清理一次 + lifespan 定时任务（session_cleanup_interval_hours）
- 幂等：按时间过滤，重复执行无副作用；失败仅记日志不中断服务
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
    """执行两层清理，返回物理删除数（软删数在日志记录）。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db = SessionLocal()
    try:
        # ① 活跃超期 → 逻辑删除
        soft_cutoff = now - timedelta(days=settings.session_retention_days)
        stale = db.scalars(
            select(ChatSession).where(
                ChatSession.deleted_at.is_(None), ChatSession.updated_at < soft_cutoff
            )
        ).all()
        for s in stale:
            s.deleted_at = now
        if stale:
            logger.info(
                "逻辑删除过期活跃会话 %d 个（保留 %d 天）",
                len(stale), settings.session_retention_days,
            )

        # ② 已软删超期 → 物理删除（含消息级联 + 长期记忆）
        purge_cutoff = now - timedelta(days=settings.session_purge_days)
        purged = db.scalars(
            select(ChatSession).where(
                ChatSession.deleted_at.is_not(None), ChatSession.deleted_at < purge_cutoff
            )
        ).all()
        for s in purged:
            long_term_memory.clear_session(s.id, s.knowledge_base_id)
            db.delete(s)
        db.commit()
        if purged:
            logger.info(
                "物理清理已删除会话 %d 个（软删超 %d 天）",
                len(purged), settings.session_purge_days,
            )
        return len(purged)
    except Exception:
        logger.exception("清理过期会话失败")
        return 0
    finally:
        db.close()
