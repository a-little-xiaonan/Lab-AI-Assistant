"""数据库连接与依赖注入：SQLite（本地零依赖）或 MySQL（Docker 部署）。

方言差异只在文件内处理：check_same_thread 是 SQLite 专有参数；
MySQL 不自动建库，启动时连 server 层执行 CREATE DATABASE IF NOT EXISTS。
"""
from __future__ import annotations

import logging

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models.database import Base, KnowledgeBase

logger = logging.getLogger(__name__)

_url = make_url(settings.database_url_resolved)
_is_sqlite = _url.get_backend_name() == "sqlite"

if _is_sqlite:
    engine = create_engine(
        settings.database_url_resolved,
        connect_args={"check_same_thread": False},  # FastAPI 多线程访问
    )
else:
    engine = create_engine(settings.database_url_resolved, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _ensure_mysql_database() -> None:
    """MySQL 的 CREATE TABLE 需要库已存在：先连 server 层建库（utf8mb4）。

    SQLite 跳过（文件即库）；URL 里没带库名时跳过。
    直接用 pymysql 裸连（SQLAlchemy 的 URL.set(database=None) 实测不会移除
    database 组件，绕开它）。
    """
    url = make_url(settings.database_url_resolved)
    if url.get_backend_name() == "sqlite" or not url.database:
        return
    import pymysql

    conn = pymysql.connect(
        host=url.host,
        port=url.port or 3306,
        user=url.username,
        password=url.password or "",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{url.database}` CHARACTER SET utf8mb4"
            )
        conn.commit()
    finally:
        conn.close()


def _seed_default_knowledge_base() -> None:
    """kb_default 隐式库显式化：幂等种子（Phase 1 数据已是 kb_default 字符串，天然对齐）。"""
    with SessionLocal() as db:
        exists = db.get(KnowledgeBase, "kb_default")
        if exists is not None:
            return
        db.add(
            KnowledgeBase(
                id="kb_default",
                name="默认知识库",
                description="系统默认知识库（Phase 1 数据兼容），可上传文档但不可删除",
                embedding_model=settings.embedding_model,
            )
        )
        db.commit()


def _migrate_add_session_name() -> None:
    """增量迁移：sessions 表加 name 列（create_all 不动已存在表）。

    幂等：inspect 检查列是否存在，不存在才 ALTER（MySQL/SQLite 都支持 ADD COLUMN）。
    """
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(engine)
    if "sessions" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("sessions")}
    if "name" in cols:
        return
    with SessionLocal() as db:
        db.execute(text("ALTER TABLE sessions ADD COLUMN name VARCHAR(255) NULL"))
        db.commit()
    logger.info("迁移完成：sessions 表新增 name 列")


def _migrate_and_cleanup() -> None:
    """启动期一次性迁移与清扫（全部幂等，重复执行无副作用）。

    1. 状态语义迁移：Phase 1 的 indexed → Phase 2 协议的 ready
    2. stale processing 清扫：上次运行中断的文档标记 failed（服务重启，处理中断）
    """
    with SessionLocal() as db:
        result = db.execute(text("UPDATE documents SET status='ready' WHERE status='indexed'"))
        if result.rowcount:
            logger.info("状态迁移：%d 条 indexed → ready", result.rowcount)
        result = db.execute(
            text("UPDATE documents SET status='failed', error_message='服务重启，处理中断' "
                 "WHERE status='processing'")
        )
        if result.rowcount:
            logger.warning("清扫 %d 条中断的 processing 文档 → failed", result.rowcount)
        # reindexing 中断（Phase 3-05）：回 ready——live 索引与 DB chunk 记录仍是旧的一致状态
        result = db.execute(
            text("UPDATE documents SET status='ready' WHERE status='reindexing'")
        )
        if result.rowcount:
            logger.warning("清扫 %d 条中断的 reindexing 文档 → ready", result.rowcount)
        db.commit()


def init_db() -> None:
    _ensure_mysql_database()
    Base.metadata.create_all(engine)
    _migrate_add_session_name()
    _seed_default_knowledge_base()
    _migrate_and_cleanup()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
