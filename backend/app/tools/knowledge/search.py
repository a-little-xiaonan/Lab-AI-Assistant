"""内部知识能力：在用户可读范围内检索并签发文档句柄。"""
from pydantic import BaseModel, ConfigDict, Field
from secrets import token_urlsafe

from app.core import rag_pipeline
from app.core.agent.models import ToolContext, ToolError, ToolResult


class KnowledgeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, max_length=4000)


def search_knowledge(args: KnowledgeArgs, ctx: ToolContext) -> ToolResult:
    if not ctx.readable_kb_ids:
        return ToolResult(status="no_data", data={"reason": "no_readable_scope", "chunks": []})
    result = rag_pipeline.prepare_evidence(
        args.question, list(ctx.readable_kb_ids), ctx.history, strict=True,
    )
    def issue_handle(chunk):
        version_id = chunk.metadata.get("document_version_id")
        handle = None
        if version_id and chunk.metadata.get("doc_id") and chunk.metadata.get("chunk_index") is not None:
            handle = f"docres_{token_urlsafe(18)}"
            ctx.resource_handles[handle] = {
                "kind": "document_chunk", "session_id": ctx.session_id,
                "kb_id": chunk.metadata.get("knowledge_base_id"), "doc_id": chunk.metadata["doc_id"],
                "document_version_id": version_id, "chunk_index": chunk.metadata["chunk_index"],
                "chunk_id": chunk.chunk_id,
            }
        return handle

    chunks = []
    for chunk in result.chunks:
        handle = issue_handle(chunk)
        chunks.append({"text": chunk.text, "source_file": chunk.source_file, "page": chunk.page,
                       "chunk_id": chunk.chunk_id, "knowledge_base_id": chunk.metadata.get("knowledge_base_id"),
                       "document_handle": handle})
    supported_ids = {chunk.chunk_id for chunk in result.chunks}
    expandable = []
    for chunk in result.candidates:
        if chunk.chunk_id in supported_ids:
            continue
        handle = issue_handle(chunk)
        if handle:
            # 弱候选只提供定位信息；正文不进入模型上下文或最终回答。
            expandable.append({"document_handle": handle, "page": chunk.page,
                               "chunk_index": chunk.metadata.get("chunk_index")})
    return ToolResult(status=result.status, data={
        "question": args.question,
        "evidence_status": "supported" if result.chunks else "insufficient",
        "decision": result.decision.as_dict(),
        "partial_failure": bool(result.failures),
        "chunks": chunks,
        "expandable_documents": expandable,
    }, error=ToolError(code="knowledge_service_failed", message="部分知识检索或核验失败，无法确认相关资料。")
       if result.failures else None)
