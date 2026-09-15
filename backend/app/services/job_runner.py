"""后台任务执行器：负责状态、心跳、取消和业务函数分派。"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass

from app.config import ROOT
from app.models.database import BackgroundJob, utcnow
from app.store.db import SessionLocal

logger = logging.getLogger(__name__)
_local_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_key(job_type: str, payload: dict) -> str | None:
    if job_type in {"kb_reindex", "document_reindex"}:
        return f"lock:kb:{payload['kb_id']}:index-write"
    if job_type == "document_publish":
        from app.models.database import Document
        with SessionLocal() as db:
            doc = db.get(Document, payload["doc_id"])
            return f"lock:kb:{doc.kb_id}:index-write" if doc else None
    if job_type == "document_prepare":
        return f"lock:document:{payload['doc_id']}:prepare"
    return None


@contextmanager
def _resource_lock(key: str | None):
    if not key:
        yield; return
    from app.config import settings
    if settings.task_mode == "rq":
        from redis import Redis
        lock = Redis.from_url(settings.redis_url).lock(
            key, timeout=settings.job_lock_ttl_seconds,
            blocking_timeout=5,
        )
        if not lock.acquire():
            raise RuntimeError("资源正在被其他任务处理")
        try: yield
        finally:
            try: lock.release()
            except Exception: logger.warning("Redis 任务锁已过期：%s", key)
    else:
        with _locks_guard:
            lock = _local_locks.setdefault(key, threading.Lock())
        if not lock.acquire(timeout=5):
            raise RuntimeError("资源正在被其他任务处理")
        try: yield
        finally: lock.release()


@dataclass
class JobContext:
    job_id: str

    def progress(self, current: int, total: int) -> None:
        with SessionLocal() as db:
            row = db.get(BackgroundJob, self.job_id)
            if row:
                row.progress_current, row.progress_total = current, total
                row.heartbeat_at = utcnow()
                db.commit()

    def cancelled(self) -> bool:
        with SessionLocal() as db:
            row = db.get(BackgroundJob, self.job_id)
            return bool(row and row.cancel_requested)


def _execute(context: JobContext, job_type: str, payload: dict) -> dict:
    if context.cancelled():
        raise InterruptedError("任务已取消")
    if job_type == "document_prepare":
        from app.core.document_processing import process_document
        process_document(payload["doc_id"])
        return {"doc_id": payload["doc_id"]}
    if job_type == "document_publish":
        from app.core.document_publisher import publish_document
        publish_document(payload["doc_id"], payload["actor_id"],
                         payload["expected_lock_version"], payload.get("comment"))
        return {"doc_id": payload["doc_id"]}
    if job_type in {"document_reindex", "kb_reindex"}:
        from app.core.reindex import reindex_manager
        reindex_manager.run(payload["kb_id"], payload.get("doc_id"))
        return {"kb_id": payload["kb_id"], "doc_id": payload.get("doc_id")}
    if job_type == "evaluation":
        command = [sys.executable, str(ROOT / "backend/scripts/eval_recruitment.py"),
                   "--run-id", payload["run_id"], "--mode", payload["mode"],
                   "--split", payload["split"]]
        if payload.get("kb_id"):
            command.extend(["--kb-id", payload["kb_id"]])
        completed = subprocess.run(command, cwd=ROOT / "backend", capture_output=True,
                                   text=True, timeout=7200)
        if completed.returncode:
            raise RuntimeError(completed.stderr[-500:] or completed.stdout[-500:] or "评测失败")
        return {"run_id": payload["run_id"]}
    if job_type == "document_expiry_scan":
        from sqlalchemy import func, select
        from app.models.database import Document
        with SessionLocal() as db:
            expired = db.scalar(select(func.count(Document.id)).where(
                Document.expires_at.is_not(None), Document.expires_at <= utcnow(),
                Document.deleted_at.is_(None),
            )) or 0
        return {"expired_documents": int(expired)}
    raise ValueError(f"未知任务类型：{job_type}")


def run_job(job_id: str) -> None:
    with SessionLocal() as db:
        row = db.get(BackgroundJob, job_id)
        if row is None or row.status not in {"queued", "running"}:
            return
        if row.cancel_requested:
            row.status, row.active_key, row.finished_at = "cancelled", None, utcnow()
            db.commit()
            return
        row.status = "running"
        row.attempt += 1
        row.started_at = row.started_at or utcnow()
        row.heartbeat_at = utcnow()
        payload = json.loads(row.payload_json or "{}")
        job_type = row.job_type
        db.commit()
    try:
        with _resource_lock(_lock_key(job_type, payload)):
            result = _execute(JobContext(job_id), job_type, payload)
        with SessionLocal() as db:
            row = db.get(BackgroundJob, job_id)
            row.status = "cancelled" if row.cancel_requested else "succeeded"
            row.result_json = json.dumps(result, ensure_ascii=False)
            row.active_key = None
            row.finished_at = row.heartbeat_at = utcnow()
            db.commit()
    except InterruptedError as exc:
        _finish_failed(job_id, "cancelled", "job_cancelled", str(exc))
    except Exception as exc:
        logger.exception("后台任务失败：job=%s type=%s", job_id, job_type)
        _finish_failed(job_id, "failed", type(exc).__name__, str(exc)[:1000])


def _finish_failed(job_id: str, status: str, code: str, message: str) -> None:
    with SessionLocal() as db:
        row = db.get(BackgroundJob, job_id)
        if row:
            row.status, row.active_key = status, None
            row.error_code, row.error_message = code, message
            row.finished_at = row.heartbeat_at = utcnow()
            db.commit()
