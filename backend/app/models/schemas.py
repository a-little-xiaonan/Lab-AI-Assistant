"""API 请求/响应模型（对齐设计文档 §8）。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


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
