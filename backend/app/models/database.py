"""SQLAlchemy ORM 模型：会话、消息、文档登记表。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """naive UTC：MySQL DATETIME 不支持带时区的 datetime（SQLite 兼容）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    """登录用户：密码仅保存 Argon2 hash；角色由 user_roles 多对多维护。"""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # user_ 前缀 UUID
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    nickname: Mapped[str] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    roles: Mapped[list["Role"]] = relationship(secondary="user_roles", back_populates="users")


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    users: Mapped[list[User]] = relationship(secondary="user_roles", back_populates="roles")


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_roles_user_role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ChatSession(Base):
    """对话会话：MVP 单知识库，knowledge_base_id 固定 kb_default（Phase 2-02 放开）。

    name：会话标题。首轮对话后由 AI 自动生成；用户手动改名后 AI 不再覆盖（用户优先）。
    deleted_at：逻辑删除标记（NULL=活跃，非空=已删除可恢复）；物理清理由
    session_cleanup 按 purge 天数执行。
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sess_ 前缀 UUID
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    anonymous_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    knowledge_base_id: Mapped[str] = mapped_column(String(64), default="kb_default")
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name_source: Mapped[str] = mapped_column(String(16), default="ai")  # ai / user / system
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class Message(Base):
    """聊天消息：user 与 assistant 都落库（短期记忆/长期记忆的数据源）。"""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user / assistant
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    session: Mapped[ChatSession] = relationship(back_populates="messages")


class KnowledgeBase(Base):
    """知识库（Phase 2-02）：kb_default 为系统默认库（init_db 幂等种子创建，禁止删除）。

    不配 documents relationship：documents.kb_id 无数据库外键（既有表无迁移框架），
    级联删除由 API 层显式执行（见 knowledge_base.delete_knowledge_base）。
    """

    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # kb_ 前缀 UUID
    name: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # access_level 是当前授权主规则：guest / student / editor / admin。
    # visibility 与 ACL 表仅为历史数据兼容保留，不再参与新的访问判定。
    access_level: Mapped[str] = mapped_column(String(16), default="guest", index=True)
    visibility: Mapped[str] = mapped_column(String(16), default="public", index=True)
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    embedding_model: Mapped[str] = mapped_column(String(64), default="text-embedding-v3")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class KnowledgeBaseRolePermission(Base):
    __tablename__ = "knowledge_base_role_permissions"
    __table_args__ = (UniqueConstraint("kb_id", "role_id", "permission", name="uq_kb_role_permission"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id"), index=True)
    permission: Mapped[str] = mapped_column(String(16))  # read / write / manage
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class KnowledgeBaseUserPermission(Base):
    __tablename__ = "knowledge_base_user_permissions"
    __table_args__ = (UniqueConstraint("kb_id", "user_id", "permission", name="uq_kb_user_permission"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    permission: Mapped[str] = mapped_column(String(16))  # read / write / manage
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Document(Base):
    """文档登记表：上传去重、索引状态、chunk 统计。

    kb_id 不设数据库外键（既有表无迁移框架，create_all 不会补约束），
    由 API 层操作前校验 + 应用层级联保证一致性；ORM 层仍配 relationship。
    """

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # doc_ 前缀 UUID
    kb_id: Mapped[str] = mapped_column(String(64), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_hash: Mapped[str] = mapped_column(String(32), index=True)
    file_size: Mapped[int] = mapped_column(Integer, default=0)  # 字节
    file_path: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), default="processing")  # processing/ready/failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    chunks: Mapped[list["ChunkRecord"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    topics: Mapped[list["DocumentTopic"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentTopic(Base):
    """文档主题：AI 初标进入 pending，管理员审核为 approved 后才参与定向检索。"""

    __tablename__ = "document_topics"
    __table_args__ = (UniqueConstraint("doc_id", "topic_code", name="uq_document_topics_doc_topic"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    topic_code: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")  # manual / filename_rule / llm_suggested
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    review_status: Mapped[str] = mapped_column(String(16), default="approved", index=True)  # pending / approved / rejected
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    document: Mapped[Document] = relationship(back_populates="topics")


class ChunkRecord(Base):
    """chunk 明细表：内容与大小的可查询副本（与 ChromaDB 双写，同事务保证一致）。

    id 与 ChromaDB 的 chunk id 一致（{doc_id}_{chunk_index}），便于两边对应。
    文本在 ChromaDB 也有（向量检索用），此表为 SQL 查询/统计/前端展示服务。
    """

    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("doc_id", "chunk_index", name="uq_chunks_doc_index"),)

    id: Mapped[str] = mapped_column(String(96), primary_key=True)  # {doc_id}_{chunk_index}
    doc_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    kb_id: Mapped[str] = mapped_column(String(64), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    char_length: Mapped[int] = mapped_column(Integer, default=0)  # 字符数
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)  # 粗估算（与检索截断同算法）
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    slide_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sheet_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    row_range: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)  # 重索引/重传时更新

    document: Mapped[Document] = relationship(back_populates="chunks")


class UserMemory(Base):
    """用户级长期记忆的可管理副本；向量保存在 ChromaDB user_memories 集合。"""

    __tablename__ = "user_memories"
    __table_args__ = (UniqueConstraint("user_id", "content_hash", name="uq_user_memory_content"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    memory_type: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(40), index=True)
    confidence: Mapped[float] = mapped_column(default=0.0)
    source_session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id"), nullable=True)
    scope_kb_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
