"""FastAPI 入口：应用实例、CORS、路由注册、统一异常处理。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# 应用日志级别：root 默认 WARNING 会吞掉全部 logger.info（检索观测、索引重建等）。
# uvicorn 先于本模块配置了 root handler，basicConfig 可能 no-op，故显式 setLevel 兜底
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger().setLevel(logging.INFO)

from app.api import chat, documents, health, knowledge_base, memory, stats
from app.api.errors import ApiError
from app.config import ensure_data_dirs, settings
from app.llm.errors import LLMError
from app.store.db import init_db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_data_dirs()
    init_db()
    logger.info("服务启动：数据目录就绪，数据库已初始化")
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="RAG AI Assistant", version="0.1.0", lifespan=lifespan)

    # 开发期放开前端本地端口；生产收敛白名单（Phase 4）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 路由统一 /api 前缀
    for router in (health.router, stats.router, documents.router, chat.router,
                   knowledge_base.router, memory.router):
        app.include_router(router, prefix="/api")

    @app.exception_handler(ApiError)
    async def api_error_handler(_req: Request, exc: ApiError):
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
