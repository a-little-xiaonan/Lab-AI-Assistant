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


class ChatSession(Base):
    """对话会话：MVP 单知识库，knowledge_base_id 固定 kb_default（Phase 2-02 放开）。"""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sess_ 前缀 UUID
    knowledge_base_id: Mapped[str] = mapped_column(String(64), default="kb_default")
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


class Document(Base):
    """文档登记表：上传去重、索引状态、chunk 统计。"""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # doc_ 前缀 UUID
    kb_id: Mapped[str] = mapped_column(String(64), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    file_hash: Mapped[str] = mapped_column(String(32), index=True)
    file_size: Mapped[int] = mapped_column(Integer, default=0)  # 字节
    file_path: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), default="processing")  # processing/indexed/failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    chunks: Mapped[list["ChunkRecord"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


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

    document: Mapped[Document] = relationship(back_populates="chunks")
