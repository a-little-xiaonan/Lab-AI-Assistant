"""服务启动时恢复超时任务；RQ 模式重新投递，inline 模式重新执行。"""
from __future__ import annotations

from datetime import timedelta
from sqlalchemy import select

from app.config import settings
from app.models.database import BackgroundJob, utcnow
from app.services.jobs.job_queue import retry_job
from app.store.db import SessionLocal


def recover_stale_jobs() -> dict:
    cutoff = utcnow() - timedelta(seconds=settings.job_stale_seconds)
    with SessionLocal() as db:
        rows = list(db.scalars(select(BackgroundJob).where(
            BackgroundJob.status == "running",
            BackgroundJob.heartbeat_at < cutoff,
        )))
        ids = []
        for row in rows:
            row.status, row.active_key = "failed", None
            row.error_code, row.error_message = "worker_lost", "Worker 心跳超时，等待恢复"
            ids.append(row.id)
        db.commit()
    recovered, failed = 0, 0
    for job_id in ids:
        try:
            retry_job(job_id); recovered += 1
        except Exception:
            failed += 1
    return {"stale": len(ids), "recovered": recovered, "failed": failed}
