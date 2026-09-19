"""管理员质量评测任务接口。"""
from __future__ import annotations

import json
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.dependencies import require_roles
from app.models.database import EvaluationRun, User
from app.models.schemas import EvaluationStartRequest
from app.services.jobs.job_queue import enqueue_job
from app.store.db import get_db

router = APIRouter(prefix="/admin", tags=["evaluations"])


@router.post("/evaluation-runs", status_code=202)
def start_evaluation(
    body: EvaluationStartRequest,
    admin: User = Depends(require_roles("admin")),
) -> dict:
    run_id = f"eval_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:6]}"
    enqueue_job(
        "evaluation",
        "evaluation_run",
        run_id,
        admin,
        {
            "run_id": run_id,
            "mode": body.mode,
            "split": body.split,
            "kb_id": body.kb_id,
        },
        f"evaluation:{run_id}",
    )
    return {"run_id": run_id, "status": "accepted"}


@router.get("/evaluation-runs")
def list_evaluation_runs(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> dict:
    total = db.scalar(select(func.count(EvaluationRun.id))) or 0
    rows = db.scalars(
        select(EvaluationRun)
        .order_by(EvaluationRun.started_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {
        "total": total,
        "items": [
            {
                "id": row.id,
                "status": row.status,
                "mode": row.mode,
                "dataset_version": row.dataset_version,
                "dataset_split": row.dataset_split,
                "kb_snapshot": row.kb_snapshot,
                "git_commit": row.git_commit,
                "metrics": json.loads(row.metrics_json or "{}"),
                "result_path": row.result_path,
                "error_message": row.error_message,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
            }
            for row in rows
        ],
    }
