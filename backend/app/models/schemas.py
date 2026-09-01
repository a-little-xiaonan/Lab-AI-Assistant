"""API 请求/响应模型（对齐设计文档 §8）。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    session_id: str | None = None      # 不传则后端自动创建会话
    knowledge_base_id: str = "kb_default"
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


class UserStatusUpdate(BaseModel):
    status: str = Field(pattern="^(active|disabled)$")


class UserRolesUpdate(BaseModel):
    roles: list[str] = Field(min_length=1)


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


class SessionCreate(BaseModel):
    knowledge_base_id: str = "kb_default"
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
    visibility: str = Field(default="public", pattern="^(public|authenticated|restricted)$")


class KnowledgeBaseOut(BaseModel):
    id: str
    name: str
    description: str | None = None
    embedding_model: str
    visibility: str = "public"
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
    topics: list[str] = []
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class DocumentTopicsUpdate(BaseModel):
    """管理员手动主题标注；主题 code 必须来自 retrieval_topics 配置。"""

    topic_codes: list[str] = Field(default_factory=list, max_length=8)


class KnowledgeBaseDetailOut(KnowledgeBaseOut):
    documents: list[DocumentOut] = []


class UploadDocumentOut(BaseModel):
    doc_id: str
    filename: str
    status: str  # processing（异步处理中）
    file_size: int
    kb_id: str


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
