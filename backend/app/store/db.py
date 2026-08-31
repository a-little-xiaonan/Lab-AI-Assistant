"""数据库连接与依赖注入：SQLite（本地零依赖）或 MySQL（Docker 部署）。

方言差异只在文件内处理：check_same_thread 是 SQLite 专有参数；
MySQL 不自动建库，启动时连 server 层执行 CREATE DATABASE IF NOT EXISTS。
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models.database import Base

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


def init_db() -> None:
    _ensure_mysql_database()
    Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
