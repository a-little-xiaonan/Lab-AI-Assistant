"""统一审计服务：关键操作同事务写入，非关键日志允许 best-effort。"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import uuid4

from app.authorization.policy import effective_level
from app.config import settings
from app.middleware.request_context import current_request_id
from app.models.database import AuditLog, User

logger = logging.getLogger(__name__)

_SENSITIVE_KEYS = {
    "password", "password_hash", "token", "access_token", "refresh_token",
    "api_key", "jwt_secret", "database_url", "prompt", "memory", "content",
}


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[已脱敏]" if str(key).lower() in _SENSITIVE_KEYS else _sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value[:50]]
    if isinstance(value, str):
        return value[:500]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)[:500]


def hash_ip(ip: str | None) -> str | None:
    if not ip:
        return None
    salt = settings.audit_hash_salt or settings.jwt_secret
    if not salt:
        return None
    return hashlib.sha256(f"{salt}:{ip}".encode("utf-8")).hexdigest()


def record_in_transaction(
    db,
    actor: User | None,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    *,
    result: str = "success",
    reason_code: str | None = None,
    detail: dict | None = None,
    request_id: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> AuditLog:
    row = AuditLog(
        id=f"audit_{uuid4().hex[:16]}",
        request_id=request_id or current_request_id(),
        actor_user_id=actor.id if actor else None,
        actor_role=effective_level(actor),
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        result=result,
        reason_code=reason_code,
        detail_json=json.dumps(_sanitize(detail or {}), ensure_ascii=False),
        ip_hash=hash_ip(ip),
        user_agent_summary=(user_agent or "")[:255] or None,
    )
    db.add(row)
    return row


def record_best_effort(*args, **kwargs) -> None:
    from app.store.db import SessionLocal

    try:
        with SessionLocal() as db:
            record_in_transaction(db, *args, **kwargs)
            db.commit()
    except Exception:
        action = kwargs.get("action") or (args[1] if len(args) > 1 else "unknown")
        logger.exception("非关键审计写入失败：action=%s", action)
