"""系统管理员：用户状态与角色管理。"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.auth import user_out
from app.api.errors import BadRequestError, NotFoundError
from app.auth.dependencies import require_roles
from app.models.database import AuditLog, EvaluationRun, Role, User, UserRole
from app.models.schemas import EvaluationStartRequest, UserOut, UserRolesUpdate, UserStatusUpdate
from app.config import ROOT
from app.store.db import get_db
from app.services.audit import record_in_transaction

router = APIRouter(prefix="/admin", tags=["admin"])


def _audit_out(row: AuditLog) -> dict:
    return {
        "id": row.id, "request_id": row.request_id, "actor_user_id": row.actor_user_id,
        "actor_role": row.actor_role, "action": row.action,
        "resource_type": row.resource_type, "resource_id": row.resource_id,
        "result": row.result, "reason_code": row.reason_code,
        "detail": json.loads(row.detail_json or "{}"), "ip_hash": row.ip_hash,
        "user_agent_summary": row.user_agent_summary, "created_at": row.created_at,
    }


@router.get("/users", response_model=list[UserOut])
def list_users(
    _admin: User = Depends(require_roles("admin")), db: Session = Depends(get_db)
) -> list[UserOut]:
    return [user_out(user) for user in db.scalars(select(User).order_by(User.created_at.desc()))]


@router.put("/users/{user_id}/status", response_model=UserOut)
def update_user_status(
    user_id: str,
    body: UserStatusUpdate,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> UserOut:
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError("user_not_found", "用户不存在")
    if user.id == admin.id and body.status != "active":
        raise BadRequestError("cannot_disable_self", "不能禁用当前管理员账号")
    user.status = body.status
    record_in_transaction(db, admin, "user.update_status", "user", user.id,
                          detail={"status": body.status})
    db.commit()
    db.refresh(user)
    return user_out(user)


@router.put("/users/{user_id}/roles", response_model=UserOut)
def update_user_roles(
    user_id: str,
    body: UserRolesUpdate,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> UserOut:
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError("user_not_found", "用户不存在")
    roles = db.scalars(select(Role).where(Role.code.in_(body.roles))).all()
    if len(roles) != len(set(body.roles)):
        raise BadRequestError("role_not_found", "存在无效角色")
    if user.id == admin.id and "admin" not in {role.code for role in roles}:
        raise BadRequestError("cannot_remove_own_admin", "不能移除当前管理员的 admin 角色")
    db.query(UserRole).filter(UserRole.user_id == user.id).delete()
    for role in roles:
        db.add(UserRole(user_id=user.id, role_id=role.id))
    record_in_transaction(db, admin, "user.update_roles", "user", user.id,
                          detail={"roles": body.roles})
    db.commit()
    db.refresh(user)
    return user_out(user)


@router.get("/audit-logs")
def list_audit_logs(
    offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
    actor_user_id: str | None = None, action: str | None = None,
    resource_type: str | None = None, resource_id: str | None = None,
    result: str | None = None, request_id: str | None = None,
    started_at: datetime | None = None, ended_at: datetime | None = None,
    _admin: User = Depends(require_roles("admin")), db: Session = Depends(get_db),
) -> dict:
    conditions = []
    for column, value in (
        (AuditLog.actor_user_id, actor_user_id), (AuditLog.action, action),
        (AuditLog.resource_type, resource_type), (AuditLog.resource_id, resource_id),
        (AuditLog.result, result), (AuditLog.request_id, request_id),
    ):
        if value:
            conditions.append(column == value)
    if started_at:
        conditions.append(AuditLog.created_at >= started_at)
    if ended_at:
        conditions.append(AuditLog.created_at <= ended_at)
    total = db.scalar(select(func.count(AuditLog.id)).where(*conditions)) or 0
    rows = db.scalars(select(AuditLog).where(*conditions).order_by(
        AuditLog.created_at.desc()).offset(offset).limit(limit)).all()
    return {"total": total, "offset": offset, "limit": limit,
            "items": [_audit_out(row) for row in rows]}


@router.get("/audit-logs/{audit_id}")
def get_audit_log(audit_id: str, _admin: User = Depends(require_roles("admin")),
                  db: Session = Depends(get_db)) -> dict:
    row = db.get(AuditLog, audit_id)
    if row is None:
        raise NotFoundError("audit_log_not_found", "审计记录不存在")
    return _audit_out(row)


def _run_evaluation(run_id: str, body: EvaluationStartRequest) -> None:
    """M3 暂用后台子进程；M4 会替换为 Redis/RQ 持久任务。"""
    script = ROOT / "backend/scripts/eval_recruitment.py"
    command = [sys.executable, str(script), "--run-id", run_id,
               "--mode", body.mode, "--split", body.split]
    if body.kb_id:
        command.extend(["--kb-id", body.kb_id])
    log_path = ROOT / "data/eval" / f"{run_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as output:
        subprocess.run(command, cwd=ROOT / "backend", stdout=output,
                       stderr=subprocess.STDOUT, check=False)


@router.post("/evaluation-runs", status_code=202)
def start_evaluation(body: EvaluationStartRequest, background_tasks: BackgroundTasks,
                     admin: User = Depends(require_roles("admin"))) -> dict:
    run_id = f"eval_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:6]}"
    from app.services.job_queue import enqueue_job
    enqueue_job(
        "evaluation", "evaluation_run", run_id, admin,
        {"run_id": run_id, "mode": body.mode, "split": body.split, "kb_id": body.kb_id},
        f"evaluation:{run_id}",
    )
    return {"run_id": run_id, "status": "accepted"}


@router.get("/evaluation-runs")
def list_evaluation_runs(offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                         _admin: User = Depends(require_roles("admin")),
                         db: Session = Depends(get_db)) -> dict:
    total = db.scalar(select(func.count(EvaluationRun.id))) or 0
    rows = db.scalars(select(EvaluationRun).order_by(
        EvaluationRun.started_at.desc()).offset(offset).limit(limit)).all()
    return {"total": total, "items": [{
        "id": row.id, "status": row.status, "mode": row.mode,
        "dataset_version": row.dataset_version, "dataset_split": row.dataset_split,
        "kb_snapshot": row.kb_snapshot, "git_commit": row.git_commit,
        "metrics": json.loads(row.metrics_json or "{}"), "result_path": row.result_path,
        "error_message": row.error_message, "started_at": row.started_at,
        "finished_at": row.finished_at,
    } for row in rows]}
