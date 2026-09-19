"""内部知识能力：只从本请求句柄展开已发布版本的相邻分块。"""
from __future__ import annotations

from sqlalchemy import and_, or_, select
from pydantic import BaseModel, ConfigDict, Field

from app.core.agent.models import ToolContext, ToolError, ToolResult
from app.core.retrieval.ranking.evidence_gate import filter_evidence
from app.core.retrieval.retriever import RetrievedChunk
from app.models.database import ChunkRecord, Document, utcnow
from app.store.db import SessionLocal


class DocumentReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    handle: str = Field(min_length=10, max_length=128)
    before: int = Field(default=1, ge=0, le=2)
    after: int = Field(default=1, ge=0, le=2)
    question: str = Field(min_length=1, max_length=4000)


def _error(code: str, message: str, status: str = "failed") -> ToolResult:
    return ToolResult(status=status, error=ToolError(code=code, message=message))


def read_document_section(args: DocumentReadArgs, ctx: ToolContext) -> ToolResult:
    handle = ctx.resource_handles.get(args.handle)
    if not handle or handle.get("kind") != "document_chunk":
        return _error("invalid_handle", "该文档定位信息无效或已过期。", "unsupported")
    if handle["session_id"] != ctx.session_id or handle["kb_id"] not in ctx.readable_kb_ids:
        return _error("handle_not_authorized", "没有读取该文档内容的权限。", "unsupported")
    now = utcnow()
    with SessionLocal() as db:
        document = db.scalar(select(Document).where(
            Document.id == handle["doc_id"], Document.kb_id == handle["kb_id"],
            Document.status == "ready", Document.governance_status == "published",
            Document.published_version_id == handle["document_version_id"],
            Document.deleted_at.is_(None),
            or_(Document.effective_at.is_(None), Document.effective_at <= now),
            or_(Document.expires_at.is_(None), Document.expires_at > now),
        ))
        if document is None:
            return _error("resource_changed", "文档的发布状态或版本已变化，请重新检索。", "no_data")
        rows = list(db.scalars(select(ChunkRecord).where(
            ChunkRecord.doc_id == document.id,
            ChunkRecord.kb_id == document.kb_id,
            ChunkRecord.document_version_id == handle["document_version_id"],
            ChunkRecord.chunk_index >= handle["chunk_index"] - args.before,
            ChunkRecord.chunk_index <= handle["chunk_index"] + args.after,
        ).order_by(ChunkRecord.chunk_index)))
    if not rows or not any(row.id == handle["chunk_id"] for row in rows):
        return _error("resource_changed", "文档分块已变化，请重新检索。", "no_data")
    chunks = [RetrievedChunk(row.id, row.text, 1.0, {"doc_id": row.doc_id,
        "knowledge_base_id": row.kb_id, "source_file": document.filename, "page": row.page,
        "document_version_id": row.document_version_id, "chunk_index": row.chunk_index}) for row in rows]
    supported, decision = filter_evidence(args.question, chunks)
    return ToolResult(status="ok" if supported else "no_data", data={
        "evidence_status": "supported" if supported else "insufficient",
        "decision": decision.as_dict(), "document_version_id": handle["document_version_id"],
        "chunks": [{"text": chunk.text, "page": chunk.page,
                    "chunk_index": chunk.metadata["chunk_index"],
                    "document_version_id": chunk.metadata["document_version_id"]} for chunk in supported],
    }, provenance={"provider": "published_document", "valid_at": now.isoformat()},
       truncated=len(rows) >= 5)
