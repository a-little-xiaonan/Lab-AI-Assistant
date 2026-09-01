"""文档上传/列表/删除（Phase 2-02：挂到知识库下，处理后台化）。

上传流程（立即返回 202 + processing，处理由 BackgroundTasks 异步执行）：
大小校验 → 同名去重 → 存盘 → 后台 worker 解析/分块/向量化/落库。
任一环节失败：文档标记 failed（error_message 可读），上传流程不中断、不产生脏数据。

旧端点 POST/GET /api/documents 保留为 deprecated 薄包装（scripts 兼容），
新前端一律使用 /api/knowledge-bases/{id}/documents。
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.errors import BadRequestError, ConflictError, NotFoundError
from app.auth.dependencies import get_optional_current_user, require_roles
from app.authorization.permissions import require_kb_permission
from app.config import settings
from app.core.document_processing import process_document
from app.core.retriever import estimate_tokens
from app.models.database import ChunkRecord, Document, DocumentTopic, KnowledgeBase, User, utcnow
from app.models.schemas import DocumentOut, DocumentTopicsUpdate, TopicSuggestionOut, UploadDocumentOut
from app.store.db import get_db
from app.store.vector_store import vector_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])

KB_DEFAULT = "kb_default"


def _topic_map(db: Session, doc_ids: list[str]) -> dict[str, dict[str, list]]:
    """主题按审核状态拆分：只有 approved 会作为正式标签返回。"""
    if not doc_ids:
        return {}
    rows = db.execute(
        select(
            DocumentTopic.doc_id, DocumentTopic.topic_code, DocumentTopic.source,
            DocumentTopic.confidence, DocumentTopic.review_status,
        ).where(DocumentTopic.doc_id.in_(doc_ids))
    ).all()
    out: dict[str, dict[str, list]] = {
        doc_id: {"approved": [], "suggestions": []} for doc_id in doc_ids
    }
    for doc_id, topic_code, source, confidence, review_status in rows:
        entry = out.setdefault(doc_id, {"approved": [], "suggestions": []})
        if review_status == "approved":
            entry["approved"].append(topic_code)
        else:
            entry["suggestions"].append(
                TopicSuggestionOut(
                    topic_code=topic_code, source=source,
                    confidence=confidence, review_status=review_status,
                )
            )
    return out


def _new_doc_id() -> str:
    return f"doc_{uuid4().hex[:12]}"


def _persist_upload(
    db: Session, content: bytes, filename: str, kb_id: str
) -> Document:
    """公共上传步骤：校验大小 → 同名去重（failed 旧记录清理重传）→ 存盘 → 登记。

    调用方负责在返回后调度后台处理（process_document）。
    """
    if len(content) > settings.max_upload_size_mb * 1024 * 1024:
        raise BadRequestError(
            "file_too_large", f"文件超过 {settings.max_upload_size_mb}MB 大小限制"
        )
    if not filename.strip():
        raise BadRequestError("empty_filename", "缺少文件名")

    # 同名去重：仅活跃（非 failed）文档拦截；failed 旧记录 → 清理后按新上传处理
    existing = db.scalar(
        select(Document).where(Document.kb_id == kb_id, Document.filename == filename)
    )
    if existing is not None and existing.status != "failed":
        raise ConflictError(
            "duplicate_document",
            f"同名文件已存在：{existing.filename}（doc_id={existing.id}）",
        )
    if existing is not None:
        logger.info("清理 failed 旧记录后重传：%s（doc_id=%s）", existing.filename, existing.id)
        Path(existing.file_path).unlink(missing_ok=True)
        db.execute(delete(ChunkRecord).where(ChunkRecord.doc_id == existing.id))
        db.execute(delete(DocumentTopic).where(DocumentTopic.doc_id == existing.id))
        vector_store.delete_document(kb_id, existing.id)
        db.delete(existing)
        db.commit()
        # 关键词索引同步（failed 旧文档的 chunk 也在索引里）
        try:
            from app.core.keyword_index import keyword_index

            keyword_index.remove_document(kb_id, existing.id)
        except Exception:
            logger.exception("关键词索引同步失败（doc=%s）", existing.id)

    doc_id = _new_doc_id()
    save_dir = settings.uploads_dir / kb_id
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = save_dir / f"{doc_id}_{Path(filename).name}"
    save_path.write_bytes(content)
    doc = Document(
        id=doc_id,
        kb_id=kb_id,
        filename=filename,
        file_hash=hashlib.sha256(content).hexdigest()[:16],
        file_size=len(content),
        file_path=str(save_path),
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def _require_kb(db: Session, kb_id: str) -> KnowledgeBase:
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise NotFoundError("knowledge_base_not_found", f"知识库不存在：{kb_id}")
    return kb


@router.post("/knowledge-bases/{kb_id}/documents", response_model=UploadDocumentOut, status_code=202)
async def upload_document_to_kb(
    kb_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_current_user),
) -> UploadDocumentOut:
    """上传文档到指定知识库：立即返回 202 + processing，处理后台异步执行。

    重建索引进行中禁止上传（swap 会丢弃重建期间写入 live 的新文档，Phase 3-05 互斥）。
    """
    require_kb_permission(db, kb_id, user, "write")
    from app.core.reindex import reindex_manager

    if reindex_manager.is_running(kb_id):
        raise ConflictError("reindex_in_progress", "该知识库正在重建索引，请稍后再试")
    content = await file.read()
    doc = _persist_upload(db, content, file.filename or "", kb_id)
    background_tasks.add_task(process_document, doc.id)
    return UploadDocumentOut(
        doc_id=doc.id, filename=doc.filename, status="processing",
        file_size=doc.file_size, kb_id=kb_id,
    )


@router.get("/knowledge-bases/{kb_id}/documents", response_model=list[DocumentOut])
def list_kb_documents(
    kb_id: str, db: Session = Depends(get_db), user: User | None = Depends(get_optional_current_user)
) -> list[DocumentOut]:
    """知识库文档列表（含处理状态，前端轮询用）。"""
    require_kb_permission(db, kb_id, user, "read")
    docs = db.scalars(
        select(Document)
        .where(Document.kb_id == kb_id)
        .order_by(Document.created_at.desc())
    )
    docs = list(docs)
    topics = _topic_map(db, [doc.id for doc in docs])
    return [
        DocumentOut(
            doc_id=d.id,
            filename=d.filename,
            file_size=d.file_size,
            status=d.status,
            error_message=d.error_message,
            chunk_count=d.chunk_count,
            topics=topics.get(d.id, {}).get("approved", []),
            topic_suggestions=topics.get(d.id, {}).get("suggestions", []),
            created_at=d.created_at,
        )
        for d in docs
    ]


@router.get("/retrieval-topics")
def list_retrieval_topics() -> list[dict]:
    """主题配置：管理端用于标注文档；配置异常时返回空列表而非接口失败。"""
    from app.core.retrieval_topics import retrieval_topics

    return retrieval_topics.all()


@router.get("/knowledge-bases/{kb_id}/documents/{doc_id}/topics")
def get_document_topics(
    kb_id: str,
    doc_id: str,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_current_user),
) -> dict:
    require_kb_permission(db, kb_id, user, "read")
    doc = db.get(Document, doc_id)
    if doc is None or doc.kb_id != kb_id:
        raise NotFoundError("document_not_found", f"文档不存在：{doc_id}")
    details = _topic_map(db, [doc_id]).get(doc_id, {"approved": [], "suggestions": []})
    return {"doc_id": doc_id, "topic_codes": details["approved"], "topic_suggestions": details["suggestions"]}


@router.put("/knowledge-bases/{kb_id}/documents/{doc_id}/topics")
def update_document_topics(
    kb_id: str,
    doc_id: str,
    body: DocumentTopicsUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
) -> dict:
    """管理员审核主题：选中的标签批准，其余 AI 待审建议驳回。"""
    require_kb_permission(db, kb_id, user, "manage")
    doc = db.get(Document, doc_id)
    if doc is None or doc.kb_id != kb_id:
        raise NotFoundError("document_not_found", f"文档不存在：{doc_id}")
    from app.core.retrieval_topics import retrieval_topics

    valid = retrieval_topics.valid_codes(body.topic_codes)
    if set(valid) != set(body.topic_codes):
        raise BadRequestError("invalid_topic_code", "存在无效主题，请刷新主题配置后重试")
    existing = {
        row.topic_code: row
        for row in db.scalars(select(DocumentTopic).where(DocumentTopic.doc_id == doc_id))
    }
    now = utcnow()
    for code, row in existing.items():
        if code in valid:
            row.review_status = "approved"
            row.reviewed_by = user.id
            row.reviewed_at = now
            if row.source == "ai_suggested":
                row.source = "ai_approved"
        elif row.review_status == "pending":
            row.review_status = "rejected"
            row.reviewed_by = user.id
            row.reviewed_at = now
        else:
            db.delete(row)  # 管理员取消此前已批准的标签
    db.add_all(
        DocumentTopic(
            doc_id=doc_id, topic_code=code, source="manual", review_status="approved",
            reviewed_by=user.id, reviewed_at=now,
        )
        for code in valid if code not in existing
    )
    db.commit()
    return {"doc_id": doc_id, "topic_codes": valid, "reviewed": True}


@router.delete("/knowledge-bases/{kb_id}/documents/{doc_id}")
def delete_kb_document(
    kb_id: str, doc_id: str, db: Session = Depends(get_db), user: User | None = Depends(get_optional_current_user)
) -> dict:
    """删除文档：向量 + chunk 记录 + 登记 + 原文件。"""
    require_kb_permission(db, kb_id, user, "write")
    doc = db.get(Document, doc_id)
    if doc is None or doc.kb_id != kb_id:
        raise NotFoundError("document_not_found", f"文档不存在：{doc_id}")

    db.execute(delete(ChunkRecord).where(ChunkRecord.doc_id == doc_id))
    db.execute(delete(DocumentTopic).where(DocumentTopic.doc_id == doc_id))
    vector_store.delete_document(kb_id, doc_id)
    Path(doc.file_path).unlink(missing_ok=True)
    db.delete(doc)
    db.commit()
    # 关键词索引同步（缓存，异常不阻断）
    try:
        from app.core.keyword_index import keyword_index

        keyword_index.remove_document(kb_id, doc_id)
    except Exception:
        logger.exception("关键词索引同步失败（doc=%s）", doc_id)
    return {"deleted": doc_id}


# ===== 以下为 Phase 1 旧端点，保留为 deprecated 薄包装（scripts 兼容）=====

@router.post("/documents", response_model=UploadDocumentOut, status_code=202)
async def upload_document_deprecated(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    knowledge_base_id: str = KB_DEFAULT,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_current_user),
) -> UploadDocumentOut:
    """[deprecated] 等价 POST /api/knowledge-bases/{kb}/documents（Phase 2 起处理后台化）。"""
    return await upload_document_to_kb(knowledge_base_id, background_tasks, file, db, user)


@router.get("/documents", response_model=list[DocumentOut])
def list_documents_deprecated(
    knowledge_base_id: str = KB_DEFAULT,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_current_user),
) -> list[DocumentOut]:
    """[deprecated] 等价 GET /api/knowledge-bases/{kb}/documents。"""
    return list_kb_documents(knowledge_base_id, db, user)


@router.get("/documents/{doc_id}/chunks")
def list_chunks(
    doc_id: str,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_current_user),
) -> dict:
    """查看某文档的分块明细：内容、大小（字符数/token 估算）、位置元数据。"""
    doc = db.get(Document, doc_id)
    if doc is None:
        raise NotFoundError("document_not_found", f"文档不存在：{doc_id}")
    require_kb_permission(db, doc.kb_id, user, "read")
    total = db.scalar(
        select(func.count(ChunkRecord.id)).where(ChunkRecord.doc_id == doc_id)
    ) or 0
    rows = db.scalars(
        select(ChunkRecord)
        .where(ChunkRecord.doc_id == doc_id)
        .order_by(ChunkRecord.chunk_index)
        .offset(offset)
        .limit(limit)
    )
    return {
        "doc_id": doc_id,
        "filename": doc.filename,
        "total": total,
        "offset": offset,
        "limit": limit,
        "chunks": [
            {
                "chunk_index": r.chunk_index,
                "text": r.text,
                "char_length": r.char_length,
                "token_estimate": r.token_estimate,
                "page": r.page,
                "slide_number": r.slide_number,
                "sheet_name": r.sheet_name,
                "row_range": r.row_range,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }
            for r in rows
        ],
    }
