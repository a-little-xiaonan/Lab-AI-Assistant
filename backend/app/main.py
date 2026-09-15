"""FastAPI 入口：应用实例、CORS、路由注册、统一异常处理。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# 应用日志级别：root 默认 WARNING 会吞掉全部 logger.info（检索观测、索引重建等）。
# uvicorn 先于本模块配置了 root handler，basicConfig 可能 no-op，故显式 setLevel 兜底
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s [request_id=%(request_id)s]: %(message)s",
)
logging.getLogger().setLevel(logging.INFO)
_old_record_factory = logging.getLogRecordFactory()


def _record_factory(*args, **kwargs):
    record = _old_record_factory(*args, **kwargs)
    record.request_id = "-"
    return record


logging.setLogRecordFactory(_record_factory)

from app.api import admin, auth, chat, documents, feedback, health, jobs, knowledge_base, memory, reviews, role_applications, stats
from app.api.errors import ApiError
from app.config import ensure_data_dirs, settings
from app.core.session_cleanup import cleanup_expired_sessions
from app.llm.errors import LLMError
from app.middleware.request_context import RequestContextMiddleware, RequestIdFilter
from app.store.db import init_db

logger = logging.getLogger(__name__)

for handler in logging.getLogger().handlers:
    handler.addFilter(RequestIdFilter())


async def _periodic_cleanup() -> None:
    """定期清理过期会话（启动时清一次 + 按配置间隔循环）。"""
    while True:
        try:
            await asyncio.to_thread(cleanup_expired_sessions)
        except Exception:
            logger.exception("定时清理过期会话异常")
        await asyncio.sleep(settings.session_cleanup_interval_hours * 3600)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_data_dirs()
    init_db()
    from app.services.job_recovery import recover_stale_jobs
    try:
        recovery = await asyncio.to_thread(recover_stale_jobs)
    except Exception:
        logger.exception("后台任务恢复扫描失败，不阻断只读问答服务")
        recovery = {"stale": 0, "recovered": 0, "failed": 1}
    cleanup_task = asyncio.create_task(_periodic_cleanup())
    logger.info("服务启动：数据库就绪，恢复任务=%s，过期会话清理已启动", recovery)
    yield
    cleanup_task.cancel()


def create_app() -> FastAPI:
    app = FastAPI(title="RAG AI Assistant", version="0.1.0", lifespan=lifespan)

    app.add_middleware(RequestContextMiddleware)

    # 开发期放开前端本地端口；生产收敛白名单（Phase 4）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 路由统一 /api 前缀
    for router in (health.router, stats.router, auth.router, admin.router, documents.router, chat.router,
                   knowledge_base.router, memory.router, reviews.router, role_applications.router,
                   jobs.router, feedback.router):
        app.include_router(router, prefix="/api")

    @app.exception_handler(ApiError)
    async def api_error_handler(req: Request, exc: ApiError):
        if exc.status in {401, 403}:
            from app.services.audit import record_best_effort
            record_best_effort(
                None, "authorization.denied", "http_request", req.url.path,
                result="denied", reason_code=exc.code,
                ip=req.client.host if req.client else None,
                user_agent=req.headers.get("user-agent"),
            )
        return JSONResponse(status_code=exc.status, content={"detail": {"code": exc.code, "message": exc.message}})

    @app.exception_handler(LLMError)
    async def llm_error_handler(_req: Request, exc: LLMError):
        return JSONResponse(status_code=502, content={"detail": {"code": exc.code, "message": exc.message}})

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_req: Request, exc: Exception):
        logger.exception("未处理异常: %s", exc)
        return JSONResponse(status_code=500, content={"detail": {"code": "internal_error", "message": "服务内部错误"}})

    return app


app = create_app()
