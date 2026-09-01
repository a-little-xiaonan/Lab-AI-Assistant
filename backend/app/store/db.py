"""数据库连接与依赖注入：SQLite（本地零依赖）或 MySQL（Docker 部署）。

方言差异只在文件内处理：check_same_thread 是 SQLite 专有参数；
MySQL 不自动建库，启动时连 server 层执行 CREATE DATABASE IF NOT EXISTS。
"""
from __future__ import annotations

import logging

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.auth.password import hash_password
from app.models.database import Base, KnowledgeBase, Role, User, UserRole

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


def _seed_roles() -> None:
    """幂等写入系统角色；guest 是未登录状态，不入表。"""
    seed = {
        "student": ("新生", "已注册用户：访问授权知识库、管理自己的会话与记忆"),
        "editor": ("内容管理员", "维护被授权知识库的文档与索引"),
        "admin": ("系统管理员", "管理用户、角色、知识库与授权"),
    }
    with SessionLocal() as db:
        for code, (name, description) in seed.items():
            if db.scalar(text("SELECT id FROM roles WHERE code=:code"), {"code": code}):
                continue
            db.add(Role(id=f"role_{code}", code=code, name=name, description=description))
        db.commit()


def _seed_initial_admin() -> None:
    """创建部署机指定的总管理员。

    密码只允许来自未提交的 .env；为空时明确跳过，避免意外创建弱口令账号。
    账号已存在时只补 admin 角色，不覆盖用户自行修改过的密码、昵称或状态。
    """
    username = settings.initial_admin_username.strip().lower()
    password = settings.initial_admin_password
    if not username or not password:
        logger.warning("未配置 INITIAL_ADMIN_PASSWORD，跳过总管理员初始化")
        return
    with SessionLocal() as db:
        admin_role = db.scalar(text("SELECT id FROM roles WHERE code='admin'"))
        if admin_role is None:
            logger.error("admin 角色未初始化，无法创建总管理员")
            return
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            user = User(
                id=f"user_{__import__('uuid').uuid4().hex[:12]}",
                username=username,
                password_hash=hash_password(password),
                nickname=settings.initial_admin_nickname.strip() or "总管理员",
            )
            db.add(user)
            db.flush()
            logger.info("已创建总管理员账号：%s", username)
        exists = db.scalar(
            select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == admin_role)
        )
        if exists is None:
            db.add(UserRole(user_id=user.id, role_id=admin_role))
        db.commit()


def _add_column_if_missing(table: str, column: str, definition: str) -> None:
    """兼容 MySQL/SQLite 的轻量增量加列。复杂关联表由 create_all 创建。"""
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(engine)
    if table not in insp.get_table_names():
        return
    columns = {c["name"] for c in insp.get_columns(table)}
    if column in columns:
        return
    with SessionLocal() as db:
        db.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))
        db.commit()
    logger.info("迁移完成：%s 表新增 %s 列", table, column)


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


def _migrate_add_chunk_updated_at() -> None:
    """增量迁移：chunks 表加 updated_at 列（已有行回填 = created_at）。"""
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(engine)
    if "chunks" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("chunks")}
    if "updated_at" in cols:
        return
    with SessionLocal() as db:
        db.execute(text("ALTER TABLE chunks ADD COLUMN updated_at DATETIME NULL"))
        db.execute(text("UPDATE chunks SET updated_at = created_at WHERE updated_at IS NULL"))
        db.commit()
    logger.info("迁移完成：chunks 表新增 updated_at 列（回填 created_at）")


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


def _migrate_add_deleted_at() -> None:
    """增量迁移：sessions 表加 deleted_at 列（逻辑删除标记，NULL=活跃）。"""
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(engine)
    if "sessions" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("sessions")}
    if "deleted_at" in cols:
        return
    with SessionLocal() as db:
        db.execute(text("ALTER TABLE sessions ADD COLUMN deleted_at DATETIME NULL"))
        db.commit()
    logger.info("迁移完成：sessions 表新增 deleted_at 列（逻辑删除）")


def _migrate_multi_user_columns() -> None:
    """Phase 4-02：扩展既有 sessions / knowledge_bases。

    旧表不强加外键，以避免 SQLite/MySQL 已有数据迁移失败；新建表自身有 FK。
    历史会话 user_id 均为空，默认作为未认领 legacy 数据，不会在认证后暴露给新用户。
    """
    _add_column_if_missing("sessions", "user_id", "VARCHAR(64) NULL")
    _add_column_if_missing("sessions", "anonymous_id", "VARCHAR(64) NULL")
    _add_column_if_missing("sessions", "summary", "TEXT NULL")
    _add_column_if_missing("sessions", "name_source", "VARCHAR(16) NOT NULL DEFAULT 'ai'")
    _add_column_if_missing("knowledge_bases", "visibility", "VARCHAR(16) NOT NULL DEFAULT 'public'")
    _add_column_if_missing("knowledge_bases", "access_level", "VARCHAR(16) NOT NULL DEFAULT 'guest'")
    _add_column_if_missing("knowledge_bases", "owner_id", "VARCHAR(64) NULL")
    _add_column_if_missing("knowledge_bases", "status", "VARCHAR(16) NOT NULL DEFAULT 'active'")
    _add_column_if_missing("knowledge_bases", "updated_at", "DATETIME NULL")
    with SessionLocal() as db:
        db.execute(text("UPDATE knowledge_bases SET visibility='public' WHERE visibility IS NULL OR visibility=''"))
        # 旧可见范围的一次性映射：public→guest，authenticated→student，restricted→editor。
        db.execute(text("UPDATE knowledge_bases SET access_level=CASE visibility "
                        "WHEN 'authenticated' THEN 'student' WHEN 'restricted' THEN 'editor' "
                        "ELSE 'guest' END "
                        "WHERE access_level IS NULL OR access_level='' OR access_level='guest'"))
        db.execute(text("UPDATE knowledge_bases SET status='active' WHERE status IS NULL OR status=''"))
        db.execute(text("UPDATE knowledge_bases SET updated_at=created_at WHERE updated_at IS NULL"))
        db.commit()


def _migrate_document_topic_review() -> None:
    """主题审核字段增量迁移：旧人工标签默认视为已审核。"""
    _add_column_if_missing("document_topics", "review_status", "VARCHAR(16) NOT NULL DEFAULT 'approved'")
    _add_column_if_missing("document_topics", "reviewed_by", "VARCHAR(64) NULL")
    _add_column_if_missing("document_topics", "reviewed_at", "DATETIME NULL")
    with SessionLocal() as db:
        db.execute(text("UPDATE document_topics SET review_status='approved' "
                        "WHERE review_status IS NULL OR review_status=''"))
        db.commit()


def init_db() -> None:
    _ensure_mysql_database()
    Base.metadata.create_all(engine)
    _migrate_add_session_name()
    _migrate_add_chunk_updated_at()
    _migrate_add_deleted_at()
    _migrate_multi_user_columns()
    _migrate_document_topic_review()
    _seed_roles()
    _seed_initial_admin()
    _seed_default_knowledge_base()
    _migrate_and_cleanup()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
