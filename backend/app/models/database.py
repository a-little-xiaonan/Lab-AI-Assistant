"""SQLAlchemy ORM 模型：会话、消息、文档登记表。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
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
    """文档登记表：技术处理状态与内容治理状态相互独立。

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
    governance_status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    sensitivity_level: Mapped[str] = mapped_column(String(16), default="guest", index=True)
    uploader_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    current_version_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    published_version_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    lock_version: Mapped[int] = mapped_column(Integer, default=1)
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    legacy_unreviewed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    chunks: Mapped[list["ChunkRecord"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    topics: Mapped[list["DocumentTopic"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    versions: Mapped[list["DocumentVersion"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentVersion(Base):
    """文档不可变版本：每次重新上传生成一条，旧版本供审计和回滚。"""

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_no", name="uq_document_versions_doc_no"),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    version_no: Mapped[int] = mapped_column(Integer)
    file_path: Mapped[str] = mapped_column(String(512))
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    processing_status: Mapped[str] = mapped_column(String(16), default="processing", index=True)
    review_status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    document: Mapped[Document] = relationship(back_populates="versions")


class DocumentVersionChunk(Base):
    """待审版本分块：与正式 chunks 分离，审核前绝不进入检索索引。"""

    __tablename__ = "document_version_chunks"
    __table_args__ = (
        UniqueConstraint("document_version_id", "chunk_index", name="uq_version_chunks_version_index"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )
    doc_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    kb_id: Mapped[str] = mapped_column(String(64), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    char_length: Mapped[int] = mapped_column(Integer, default=0)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    slide_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sheet_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    row_range: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DocumentQualityCheck(Base):
    """文档版本质量检查结果；blocking 失败会阻止提交审核和发布。"""

    __tablename__ = "document_quality_checks"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )
    check_type: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    result: Mapped[str] = mapped_column(String(16), index=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DocumentReview(Base):
    """不可变审核流水：提交、预审、批准、驳回、归档和回滚均追加记录。"""

    __tablename__ = "document_reviews"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )
    reviewer_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(24), index=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


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
    document_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
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
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    actor_role: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[str] = mapped_column(String(16), default="success", index=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent_summary: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RoleApplication(Base):
    """实验室成员申请：历史永久保留，pending_key 保证每人最多一条待审批。"""

    __tablename__ = "role_applications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    target_role: Mapped[str] = mapped_column(String(32), default="editor")
    reason: Mapped[str] = mapped_column(Text)
    evidence_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    pending_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class EvaluationRun(Base):
    """评测运行索引与汇总；逐题详情保存在版本化 JSON 文件。"""

    __tablename__ = "evaluation_runs"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    mode: Mapped[str] = mapped_column(String(16))
    dataset_version: Mapped[str] = mapped_column(String(64))
    dataset_split: Mapped[str] = mapped_column(String(16), default="dev")
    kb_snapshot: Mapped[str] = mapped_column(String(128))
    git_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str] = mapped_column(String(64))
    embedding_model: Mapped[str] = mapped_column(String(64))
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BackgroundJob(Base):
    """持久后台任务：MySQL 是用户可见状态的事实源，Redis 只负责投递。"""

    __tablename__ = "background_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(32), index=True)
    resource_type: Mapped[str] = mapped_column(String(32), index=True)
    resource_id: Mapped[str] = mapped_column(String(96), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    progress_current: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    idempotency_key: Mapped[str] = mapped_column(String(255), index=True)
    active_key: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    requested_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    queue_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SuggestedQuestion(Base):
    """招新首页推荐问题；只允许人工审核后的问题对外展示。"""

    __tablename__ = "suggested_questions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    question: Mapped[str] = mapped_column(String(500))
    required_level: Mapped[str] = mapped_column(String(16), default="guest", index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    source_type: Mapped[str] = mapped_column(String(16), default="manual")
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AnswerFeedback(Base):
    """回答反馈：保留只读快照，不携带私人长期记忆。"""

    __tablename__ = "answer_feedback"
    __table_args__ = (UniqueConstraint("message_id", "identity_key", name="uq_feedback_message_identity"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    anonymous_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    identity_key: Mapped[str] = mapped_column(String(96))
    rating: Mapped[str] = mapped_column(String(16), index=True)
    reason_code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reviewer_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluation_candidate: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
