"""API 请求/响应模型（对齐设计文档 §8）。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    session_id: str | None = None      # 不传则后端自动创建会话
    knowledge_base_id: str = "kb_default"
    message: str
    stream: bool = False               # Phase 2-01 支持流式，MVP 传 true 返回 400


class SourceOut(BaseModel):
    source_file: str
    page: int | None = None
    snippet: str = ""


class ChatResponse(BaseModel):
    answer: str
    sources: list[SourceOut] = []


class SessionCreate(BaseModel):
    knowledge_base_id: str = "kb_default"


class SessionOut(BaseModel):
    id: str
    knowledge_base_id: str
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


class KnowledgeBaseOut(BaseModel):
    id: str
    name: str
    description: str | None = None
    embedding_model: str
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
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


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
