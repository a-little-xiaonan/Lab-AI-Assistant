"""管理员审计日志查询接口。"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.errors import NotFoundError
from app.auth.dependencies import require_roles
from app.models.database import AuditLog, User
from app.store.db import get_db

router = APIRouter(prefix="/admin", tags=["audit-logs"])


def _audit_out(row: AuditLog) -> dict:
    return {
        "id": row.id,
        "request_id": row.request_id,
        "actor_user_id": row.actor_user_id,
        "actor_role": row.actor_role,
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "result": row.result,
        "reason_code": row.reason_code,
        "detail": json.loads(row.detail_json or "{}"),
        "ip_hash": row.ip_hash,
        "user_agent_summary": row.user_agent_summary,
        "created_at": row.created_at,
    }


@router.get("/audit-logs")
def list_audit_logs(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    actor_user_id: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    result: str | None = None,
    request_id: str | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    _admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> dict:
    conditions = []
    for column, value in (
        (AuditLog.actor_user_id, actor_user_id),
        (AuditLog.action, action),
        (AuditLog.resource_type, resource_type),
        (AuditLog.resource_id, resource_id),
        (AuditLog.result, result),
        (AuditLog.request_id, request_id),
    ):
        if value:
            conditions.append(column == value)
    if started_at:
        conditions.append(AuditLog.created_at >= started_at)
    if ended_at:
        conditions.append(AuditLog.created_at <= ended_at)
    total = db.scalar(select(func.count(AuditLog.id)).where(*conditions)) or 0
    rows = db.scalars(
        select(AuditLog)
        .where(*conditions)
        .order_by(AuditLog.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [_audit_out(row) for row in rows],
    }


@router.get("/audit-logs/{audit_id}")
def get_audit_log(
    audit_id: str,
    _admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> dict:
    row = db.get(AuditLog, audit_id)
    if row is None:
        raise NotFoundError("audit_log_not_found", "审计记录不存在")
    return _audit_out(row)
