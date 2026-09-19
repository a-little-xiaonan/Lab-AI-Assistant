"""系统状态路由聚合。"""
from fastapi import APIRouter

from app.api.system import health, stats

router = APIRouter()
router.include_router(health.router)
router.include_router(stats.router)
