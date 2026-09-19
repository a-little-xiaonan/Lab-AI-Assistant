"""API 请求/响应模型（对齐设计文档 §8）。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    session_id: str | None = None      # 不传则后端自动创建会话
    # 不再由客户端选择知识库；保留字段仅兼容旧客户端，服务端会忽略其值。
    knowledge_base_id: str | None = None
    message: str
    stream: bool = False               # Phase 2-01 支持流式，MVP 传 true 返回 400


# ----- 认证与用户（Phase 4-02）-----

class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)
    nickname: str = Field(min_length=1, max_length=64)
    email: str | None = None


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    id: str
    username: str
    nickname: str
    email: str | None = None
    roles: list[str] = []
    status: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class UserMemoryOut(BaseModel):
    id: str
    memory_type: str
    content: str
    confidence: float
    source_session_id: str | None = None
    scope_kb_id: str | None = None
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


class UserMemoryUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=500)


class ChunkUpdate(BaseModel):
    """单个文档分块的乐观锁更新。

    expected_updated_at 必须来自最近一次分块查询，避免管理员之间静默覆盖。
    """

    text: str = Field(min_length=1, max_length=20_000)
    expected_updated_at: datetime


class UserStatusUpdate(BaseModel):
    status: str = Field(pattern="^(active|disabled)$")


class UserRolesUpdate(BaseModel):
    roles: list[str] = Field(min_length=1)


class RoleApplicationCreate(BaseModel):
    target_role: str = Field(default="editor", pattern="^editor$")
    reason: str = Field(min_length=10, max_length=1000)
    evidence_text: str | None = Field(default=None, max_length=2000)


class RoleApplicationReview(BaseModel):
    comment: str | None = Field(default=None, max_length=1000)


class RoleApplicationOut(BaseModel):
    id: str
    user_id: str
    target_role: str
    reason: str
    evidence_text: str | None = None
    status: str
    reviewed_by: str | None = None
    review_comment: str | None = None
    created_at: datetime
    updated_at: datetime
    reviewed_at: datetime | None = None
    model_config = ConfigDict(from_attributes=True)


class EvaluationStartRequest(BaseModel):
    mode: str = Field(default="retrieval", pattern="^(retrieval|full)$")
    split: str = Field(default="dev", pattern="^(dev|holdout|all)$")
    kb_id: str | None = None


class FeedbackCreate(BaseModel):
    rating: str = Field(pattern="^(helpful|unhelpful)$")
    reason_code: str | None = Field(default=None, pattern="^(wrong|outdated|bad_citation|incomplete|no_answer|permission|other)$")
    comment: str | None = Field(default=None, max_length=1000)


class FeedbackReview(BaseModel):
    resolution_note: str = Field(min_length=1, max_length=1000)


class SuggestedQuestionCreate(BaseModel):
    category: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=2, max_length=500)
    required_level: str = Field(default="guest", pattern="^(guest|student|editor|admin)$")
    sort_order: int = 0
    enabled: bool = True


class SuggestedQuestionUpdate(SuggestedQuestionCreate):
    pass


class KnowledgeBasePermissionGrant(BaseModel):
    permission: str = Field(pattern="^(read|write|manage)$")
    role_code: str | None = None
    user_id: str | None = None


class SourceOut(BaseModel):
    source_file: str
    page: int | None = None
    snippet: str = ""


class ChatResponse(BaseModel):
    answer: str
    sources: list[SourceOut] = []
    message_id: int | None = None


class SessionCreate(BaseModel):
    knowledge_base_id: str | None = None  # 保留兼容字段，服务端统一使用自动范围
    name: str | None = None  # 可选：创建时直接命名（不传则由 AI 首轮后生成）


class SessionRename(BaseModel):
    name: str = Field(min_length=1, max_length=50)  # 用户改名（AI 命名后不再覆盖）


class SessionBatchDelete(BaseModel):
    session_ids: list[str] | None = None  # 指定删除的会话；缺省或 all=true → 全部
    all: bool = False


class SessionOut(BaseModel):
    id: str
    knowledge_base_id: str
    name: str | None = None
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class SessionDetailOut(SessionOut):
    messages: list[MessageOut] = []


# ----- 知识库 / 文档（Phase 2-02）-----

class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    embedding_model: str | None = None  # 不传默认全局模型；与全局不同 → 400（每库模型选择后置）
    access_level: str = Field(default="guest", pattern="^(guest|student|editor|admin)$")


class KnowledgeBaseOut(BaseModel):
    id: str
    name: str
    description: str | None = None
    embedding_model: str
    access_level: str = "guest"
    document_count: int = 0
    chunk_count: int = 0
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class DocumentOut(BaseModel):
    doc_id: str
    filename: str
    file_size: int
    status: str  # processing / ready / failed
    error_message: str | None = None
    chunk_count: int
    governance_status: str = "draft"
    sensitivity_level: str = "guest"
    current_version: int = 1
    current_version_id: str | None = None
    published_version_id: str | None = None
    version_review_status: str | None = None
    lock_version: int = 1
    review_comment: str | None = None
    published_at: datetime | None = None
    content_owner: str | None = None
    source_name: str | None = None
    effective_at: datetime | None = None
    expires_at: datetime | None = None
    last_reviewed_at: datetime | None = None
    topics: list[str] = []
    topic_suggestions: list["TopicSuggestionOut"] = []
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class DocumentTopicsUpdate(BaseModel):
    """管理员手动主题标注；主题 code 必须来自 retrieval_topics 配置。"""

    topic_codes: list[str] = Field(default_factory=list, max_length=8)


class TopicSuggestionOut(BaseModel):
    topic_code: str
    source: str
    confidence: float | None = None
    review_status: str


class KnowledgeBaseDetailOut(KnowledgeBaseOut):
    documents: list[DocumentOut] = []


class UploadDocumentOut(BaseModel):
    doc_id: str
    filename: str
    status: str  # processing（异步处理中）
    file_size: int
    kb_id: str


class DocumentReviewAction(BaseModel):
    expected_lock_version: int = Field(ge=1)
    comment: str | None = Field(default=None, max_length=1000)


class DocumentRollbackAction(DocumentReviewAction):
    version_id: str


class DocumentGovernanceUpdate(BaseModel):
    sensitivity_level: str = Field(pattern="^(guest|student|editor|admin)$")
    content_owner: str = Field(min_length=1, max_length=128)
    source_name: str = Field(min_length=1, max_length=255)
    effective_at: datetime | None = None
    expires_at: datetime | None = None


class DocumentQualityCheckOut(BaseModel):
    check_type: str
    severity: str
    result: str
    details: dict = {}
    created_at: datetime


class DocumentReviewOut(BaseModel):
    action: str
    reviewer_id: str | None = None
    comment: str | None = None
    created_at: datetime


class DocumentVersionOut(BaseModel):
    id: str
    version_no: int
    processing_status: str
    review_status: str
    chunk_count: int
    file_size: int
    change_summary: str | None = None
    created_by: str | None = None
    created_at: datetime


# ----- 重新索引（Phase 3-05）-----

class ReindexRequest(BaseModel):
    doc_id: str | None = None  # 缺省 = 全库重建


class ReindexStatusOut(BaseModel):
    kb_id: str
    doc_id: str | None = None
    status: str  # idle / running / done / failed
    total: int = 0
    done: int = 0
    current_doc: str | None = None
    docs_before: int | None = None
    docs_after: int | None = None
    error_message: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
