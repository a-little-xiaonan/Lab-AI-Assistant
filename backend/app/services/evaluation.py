"""评测运行登记、知识库快照和脱敏配置快照。"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from sqlalchemy import func, select

from app.config import ROOT, settings
from app.models.database import ChunkRecord, Document, EvaluationRun, utcnow
from app.store.db import SessionLocal


def git_commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return None


def knowledge_snapshot(kb_id: str) -> str:
    with SessionLocal() as db:
        rows = db.execute(select(Document.id, Document.published_version_id, Document.updated_at).where(
            Document.kb_id == kb_id, Document.governance_status == "published",
            Document.deleted_at.is_(None),
        ).order_by(Document.id)).all()
        chunks = db.scalar(select(func.count(ChunkRecord.id)).where(ChunkRecord.kb_id == kb_id)) or 0
    raw = json.dumps([(a, b, str(c)) for a, b, c in rows], ensure_ascii=False)
    return f"kb-{kb_id}-{len(rows)}d-{chunks}c-{hashlib.sha1(raw.encode()).hexdigest()[:10]}"


def safe_config_snapshot() -> dict:
    return {
        "llm_model": settings.llm_model,
        "embedding_model": settings.embedding_model,
        "retrieval_top_k": settings.retrieval_top_k,
        "hybrid_retrieval_enabled": settings.hybrid_retrieval_enabled,
        "rewrite_enabled": settings.rewrite_enabled,
        "rerank_enabled": settings.rerank_enabled,
        "rerank_model": settings.rerank_model,
        "evidence_gate_enabled": settings.evidence_gate_enabled,
        "query_planning_enabled": settings.query_planning_enabled,
        "topic_retrieval_enabled": settings.topic_retrieval_enabled,
    }


def start_run(run_id: str, mode: str, dataset_version: str, split: str,
              kb_snapshot: str) -> None:
    with SessionLocal() as db:
        db.add(EvaluationRun(
            id=run_id, mode=mode, dataset_version=dataset_version,
            dataset_split=split, kb_snapshot=kb_snapshot, git_commit=git_commit(),
            model=settings.llm_model, embedding_model=settings.embedding_model,
            config_json=json.dumps(safe_config_snapshot(), ensure_ascii=False),
        ))
        db.commit()


def finish_run(run_id: str, metrics: dict, result_path: Path,
               error_message: str | None = None) -> None:
    with SessionLocal() as db:
        row = db.get(EvaluationRun, run_id)
        if row is None:
            return
        row.status = "failed" if error_message else "completed"
        row.metrics_json = json.dumps(metrics, ensure_ascii=False)
        row.result_path = str(result_path)
        row.error_message = error_message
        row.finished_at = utcnow()
        db.commit()
