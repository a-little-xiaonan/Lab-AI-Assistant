"""持久后台任务查询、重试与取消。"""
from __future__ import annotations

import json
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.auth.dependencies import require_roles
from app.authorization.policy import effective_level
from app.models.database import BackgroundJob, User
from app.services.audit import record_in_transaction
from app.services.job_queue import cancel_job, retry_job
from app.store.db import get_db

router = APIRouter(prefix="/system/jobs", tags=["jobs"])
require_operator = require_roles("editor", "admin")


def _visible(row: BackgroundJob, user: User) -> bool:
    return effective_level(user) == "admin" or row.requested_by == user.id


def _out(row: BackgroundJob) -> dict:
    return {
        "id": row.id, "job_type": row.job_type, "resource_type": row.resource_type,
        "resource_id": row.resource_id, "status": row.status,
        "progress_current": row.progress_current, "progress_total": row.progress_total,
        "attempt": row.attempt, "max_attempts": row.max_attempts,
        "requested_by": row.requested_by, "request_id": row.request_id,
        "error_code": row.error_code, "error_message": row.error_message,
        "result": json.loads(row.result_json or "{}"), "created_at": row.created_at,
        "started_at": row.started_at, "heartbeat_at": row.heartbeat_at,
        "finished_at": row.finished_at,
    }


@router.get("")
def list_jobs(status: str | None = None, job_type: str | None = None,
              offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
              user: User = Depends(require_operator), db: Session = Depends(get_db)) -> dict:
    conditions = []
    if effective_level(user) != "admin":
        conditions.append(BackgroundJob.requested_by == user.id)
    if status:
        conditions.append(BackgroundJob.status == status)
    if job_type:
        conditions.append(BackgroundJob.job_type == job_type)
    total = db.scalar(select(func.count(BackgroundJob.id)).where(*conditions)) or 0
    rows = db.scalars(select(BackgroundJob).where(*conditions).order_by(
        BackgroundJob.created_at.desc()).offset(offset).limit(limit)).all()
    return {"total": total, "items": [_out(row) for row in rows]}


@router.get("/{job_id}")
def get_job(job_id: str, user: User = Depends(require_operator),
            db: Session = Depends(get_db)) -> dict:
    row = db.get(BackgroundJob, job_id)
    if row is None or not _visible(row, user):
        raise ApiError(404, "job_not_found", "任务不存在")
    return _out(row)


@router.post("/{job_id}/retry")
def retry(job_id: str, admin: User = Depends(require_roles("admin")),
          db: Session = Depends(get_db)) -> dict:
    row = retry_job(job_id)
    record_in_transaction(db, admin, "job.retry", "background_job", job_id)
    db.commit()
    return _out(row)


@router.post("/{job_id}/cancel")
def cancel(job_id: str, admin: User = Depends(require_roles("admin")),
           db: Session = Depends(get_db)) -> dict:
    row = cancel_job(job_id)
    record_in_transaction(db, admin, "job.cancel", "background_job", job_id)
    db.commit()
    return _out(row)
