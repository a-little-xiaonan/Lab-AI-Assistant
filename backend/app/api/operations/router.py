"""系统运营路由聚合。"""
from fastapi import APIRouter

from app.api.operations import audit_logs, evaluations, jobs

router = APIRouter()
router.include_router(audit_logs.router)
router.include_router(evaluations.router)
router.include_router(jobs.router)
