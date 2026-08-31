"""pytest 全局 fixtures：TestClient + 内存 SQLite（StaticPool），覆盖 get_db 依赖。

- 测试库与生产库完全隔离：SQLAlchemy engine 用 sqlite:// 内存（StaticPool 单连接共享）
- lifespan 里的 init_db 被 monkeypatch 掉（避免连真实 MySQL）；ensure_data_dirs 无害保留
- 每用例建表/清依赖覆盖/删表，互不污染
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.models.database import Base
from app.store.db import get_db

TEST_ENGINE = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = sessionmaker(bind=TEST_ENGINE, autoflush=False, expire_on_commit=False)


def _override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def db_session():
    """直接操作测试库的会话（单元级直测用，与 client 不混用）。"""
    Base.metadata.create_all(TEST_ENGINE)
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()
    Base.metadata.drop_all(TEST_ENGINE)


@pytest.fixture(autouse=True)
def _fake_memory_manager(monkeypatch):
    """测试环境隔离：短期记忆打桩为进程内空实现（不触发 load_from_db 连真实库）。"""
    from app.memory.memory_manager import memory_manager
    from app.memory.short_term import ShortTermMemory

    def fake_get(session_id: str):
        return ShortTermMemory(session_id)

    monkeypatch.setattr(memory_manager, "get", fake_get)


@pytest.fixture()
def client(monkeypatch):
    """TestClient：内存库会话、跳过真实数据库初始化。"""
    Base.metadata.create_all(TEST_ENGINE)
    monkeypatch.setattr("app.main.init_db", lambda: None)  # 不连真实 MySQL
    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    Base.metadata.drop_all(TEST_ENGINE)
