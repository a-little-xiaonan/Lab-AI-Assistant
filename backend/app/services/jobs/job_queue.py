"""统一后台任务入口：MySQL 持久状态，inline/RQ 共用同一执行函数。"""
from __future__ import annotations

import json
import logging
import threading
from uuid import uuid4

from sqlalchemy import select

from app.api.errors import ApiError, ConflictError
from app.config import settings
from app.middleware.request_context import current_request_id
from app.models.database import BackgroundJob, User
from app.store.db import SessionLocal

logger = logging.getLogger(__name__)
ACTIVE_STATUSES = {"queued", "running"}


def enqueue_job(job_type: str, resource_type: str, resource_id: str,
                actor: User | None, payload: dict, idempotency_key: str) -> BackgroundJob:
    """创建任务并投递；同一 active_key 只允许一个活跃任务。"""
    with SessionLocal() as db:
        existing = db.scalar(select(BackgroundJob).where(
            BackgroundJob.active_key == idempotency_key
        ))
        if existing is not None:
            return existing
        row = BackgroundJob(
            id=f"job_{uuid4().hex[:20]}", job_type=job_type,
            resource_type=resource_type, resource_id=resource_id,
            idempotency_key=idempotency_key, active_key=idempotency_key,
            requested_by=actor.id if actor else None,
            payload_json=json.dumps(payload, ensure_ascii=False),
            max_attempts=settings.job_max_retries,
            request_id=current_request_id(),
        )
        db.add(row)
        try:
            db.commit()
        except Exception:
            db.rollback()
            existing = db.scalar(select(BackgroundJob).where(
                BackgroundJob.active_key == idempotency_key
            ))
            if existing is not None:
                return existing
            raise
        db.refresh(row)
        job_id = row.id

    if settings.task_mode == "rq":
        try:
            from redis import Redis
            from rq import Queue
            connection = Redis.from_url(settings.redis_url)
            queued = Queue(settings.rq_queue_name, connection=connection).enqueue(
                "app.services.jobs.job_runner.run_job", job_id,
                job_id=job_id, result_ttl=86400, failure_ttl=604800,
            )
            with SessionLocal() as db:
                current = db.get(BackgroundJob, job_id)
                current.queue_job_id = queued.id
                db.commit()
        except Exception as exc:
            with SessionLocal() as db:
                current = db.get(BackgroundJob, job_id)
                current.status, current.active_key = "failed", None
                current.error_code = "queue_unavailable"
                current.error_message = "任务队列暂不可用，请稍后重试"
                db.commit()
            raise ApiError(503, "queue_unavailable", "任务队列暂不可用，请稍后重试") from exc
    else:
        threading.Thread(target=_run_inline, args=(job_id,), daemon=True).start()

    with SessionLocal() as db:
        return db.get(BackgroundJob, job_id)


def _run_inline(job_id: str) -> None:
    from app.services.jobs.job_runner import run_job
    run_job(job_id)


def retry_job(job_id: str) -> BackgroundJob:
    with SessionLocal() as db:
        row = db.get(BackgroundJob, job_id)
        if row is None:
            raise ApiError(404, "job_not_found", "任务不存在")
        if row.status not in {"failed", "cancelled"}:
            raise ConflictError("job_not_retryable", "只有失败或已取消任务可以重试")
        if row.attempt >= row.max_attempts:
            raise ConflictError("job_attempts_exhausted", "任务重试次数已用尽")
        row.status, row.active_key = "queued", row.idempotency_key
        row.cancel_requested = False
        row.error_code = row.error_message = None
        db.commit()
        payload = json.loads(row.payload_json or "{}")
        actor_id = row.requested_by
        job_type, resource_type, resource_id, key = (
            row.job_type, row.resource_type, row.resource_id, row.idempotency_key
        )
    # 复用已有任务 ID，直接重新投递，避免产生第二条历史记录。
    if settings.task_mode == "rq":
        try:
            from redis import Redis
            from rq import Queue
            Queue(settings.rq_queue_name, connection=Redis.from_url(settings.redis_url)).enqueue(
                "app.services.jobs.job_runner.run_job", job_id, job_id=job_id,
            )
        except Exception as exc:
            raise ApiError(503, "queue_unavailable", "任务队列暂不可用") from exc
    else:
        threading.Thread(target=_run_inline, args=(job_id,), daemon=True).start()
    with SessionLocal() as db:
        return db.get(BackgroundJob, job_id)


def cancel_job(job_id: str) -> BackgroundJob:
    with SessionLocal() as db:
        row = db.get(BackgroundJob, job_id)
        if row is None:
            raise ApiError(404, "job_not_found", "任务不存在")
        if row.status not in ACTIVE_STATUSES:
            raise ConflictError("job_not_cancellable", "当前任务状态不可取消")
        row.cancel_requested = True
        if row.status == "queued":
            row.status, row.active_key = "cancelled", None
        db.commit()
        db.refresh(row)
        return row
