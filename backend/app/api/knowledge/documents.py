"""知识库文档上传、列表、删除、版本与分块查看。

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

from app.api.errors import ApiError, BadRequestError, ConflictError, NotFoundError
from app.auth.dependencies import require_roles
from app.authorization.permissions import enforce_action, require_kb_permission
from app.config import settings
from app.core.documents.lifecycle.document_processing import process_document
from app.core.documents.parsing.models import Chunk
from app.core.retrieval.retriever import estimate_tokens
from app.llm.qwen import embed_texts
from app.models.database import (
    ChunkRecord, Document, DocumentTopic, DocumentVersion, DocumentVersionChunk,
    KnowledgeBase, User, utcnow,
)
from app.models.schemas import (
    ChunkUpdate, DocumentGovernanceUpdate, DocumentOut, DocumentTopicsUpdate, TopicSuggestionOut,
    UploadDocumentOut,
)
from app.store.db import get_db
from app.store.vector_store import vector_store
from app.services.jobs.job_queue import enqueue_job

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])
require_content_manager = require_roles("editor", "admin")

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


def _document_out(db: Session, doc: Document, topic_data: dict | None = None) -> DocumentOut:
    topic_data = topic_data or {"approved": [], "suggestions": []}
    version = db.get(DocumentVersion, doc.current_version_id) if doc.current_version_id else None
    return DocumentOut(
        doc_id=doc.id, filename=doc.filename, file_size=doc.file_size,
        status=doc.status, error_message=doc.error_message, chunk_count=doc.chunk_count,
        governance_status=doc.governance_status, sensitivity_level=doc.sensitivity_level,
        current_version=doc.current_version, current_version_id=doc.current_version_id,
        published_version_id=doc.published_version_id,
        version_review_status=version.review_status if version else None,
        lock_version=doc.lock_version, review_comment=doc.review_comment,
        published_at=doc.published_at, topics=topic_data.get("approved", []),
        content_owner=doc.content_owner, source_name=doc.source_name,
        effective_at=doc.effective_at, expires_at=doc.expires_at,
        last_reviewed_at=doc.last_reviewed_at,
        topic_suggestions=topic_data.get("suggestions", []), created_at=doc.created_at,
    )


def _persist_upload(
    db: Session, content: bytes, filename: str, kb_id: str, user: User
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
            from app.core.retrieval.indexing.keyword_index import keyword_index

            keyword_index.remove_document(kb_id, existing.id)
        except Exception:
            logger.exception("关键词索引同步失败（doc=%s）", existing.id)

    doc_id = _new_doc_id()
    version_id = f"ver_{uuid4().hex[:20]}"
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
        governance_status="draft",
        sensitivity_level=db.get(KnowledgeBase, kb_id).access_level,
        uploader_id=user.id,
        current_version=1,
        current_version_id=version_id,
        content_owner=user.nickname or user.username,
        source_name=filename,
    )
    version = DocumentVersion(
        id=version_id, document_id=doc_id, version_no=1,
        file_path=str(save_path), file_hash=doc.file_hash, file_size=len(content),
        created_by=user.id,
    )
    db.add_all([doc, version])
    from app.services.operations.audit import record_in_transaction
    record_in_transaction(db, user, "document.upload", "document", doc_id,
                          detail={"kb_id": kb_id, "filename": filename, "version": 1})
    db.commit()
    db.refresh(doc)
    return doc


@router.put("/knowledge-bases/{kb_id}/documents/{doc_id}/governance", response_model=DocumentOut)
def update_document_governance(
    kb_id: str, doc_id: str, body: DocumentGovernanceUpdate,
    db: Session = Depends(get_db), user: User = Depends(require_content_manager),
) -> DocumentOut:
    kb = require_kb_permission(db, kb_id, user, "write")
    doc = db.get(Document, doc_id)
    if doc is None or doc.kb_id != kb_id:
        raise NotFoundError("document_not_found", "文档不存在")
    from app.authorization.policy import LEVEL_VALUE
    if LEVEL_VALUE[body.sensitivity_level] < LEVEL_VALUE[kb.access_level]:
        raise BadRequestError("invalid_sensitivity_level", "文档敏感等级不能低于知识库等级")
    if body.effective_at and body.expires_at and body.expires_at <= body.effective_at:
        raise BadRequestError("invalid_effective_window", "过期时间必须晚于生效时间")
    doc.sensitivity_level = body.sensitivity_level
    doc.content_owner = body.content_owner.strip()
    doc.source_name = body.source_name.strip()
    doc.effective_at = body.effective_at
    doc.expires_at = body.expires_at
    doc.last_reviewed_at = utcnow()
    doc.lock_version += 1
    from app.services.operations.audit import record_in_transaction
    record_in_transaction(db, user, "document.update_governance", "document", doc.id,
                          detail={"sensitivity_level": body.sensitivity_level})
    db.commit()
    return _document_out(db, doc, _topic_map(db, [doc.id]).get(doc.id))


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
    user: User = Depends(require_content_manager),
) -> UploadDocumentOut:
    """上传文档到指定知识库：立即返回 202 + processing，处理后台异步执行。

    重建索引进行中禁止上传（swap 会丢弃重建期间写入 live 的新文档，Phase 3-05 互斥）。
    """
    require_kb_permission(db, kb_id, user, "write")
    from app.core.documents.lifecycle.reindex import reindex_manager

    if reindex_manager.is_running(kb_id):
        raise ConflictError("reindex_in_progress", "该知识库正在重建索引，请稍后再试")
    content = await file.read()
    doc = _persist_upload(db, content, file.filename or "", kb_id, user)
    enqueue_job("document_prepare", "document", doc.id, user,
                {"doc_id": doc.id}, f"document_prepare:{doc.id}:{doc.current_version}")
    return UploadDocumentOut(
        doc_id=doc.id, filename=doc.filename, status="processing",
        file_size=doc.file_size, kb_id=kb_id,
    )


@router.post("/knowledge-bases/{kb_id}/documents/{doc_id}/versions",
             response_model=UploadDocumentOut, status_code=202)
async def upload_document_version(
    kb_id: str, doc_id: str, background_tasks: BackgroundTasks,
    file: UploadFile = File(...), db: Session = Depends(get_db),
    user: User = Depends(require_content_manager),
) -> UploadDocumentOut:
    """为已有文档创建不可变新版本；正式版本在审核发布前继续提供检索。"""
    require_kb_permission(db, kb_id, user, "write")
    doc = db.get(Document, doc_id)
    if doc is None or doc.kb_id != kb_id or doc.deleted_at is not None:
        raise NotFoundError("document_not_found", "文档不存在")
    if doc.status == "processing":
        raise ConflictError("document_processing", "文档正在处理，请稍后重试")
    content = await file.read()
    if not content or len(content) > settings.max_upload_size_mb * 1024 * 1024:
        raise BadRequestError("invalid_file_size", "文件为空或超过大小限制")
    version_no = doc.current_version + 1
    version_id = f"ver_{uuid4().hex[:20]}"
    save_dir = settings.uploads_dir / kb_id / doc.id
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = save_dir / f"v{version_no}_{Path(file.filename or doc.filename).name}"
    save_path.write_bytes(content)
    file_hash = hashlib.sha256(content).hexdigest()[:16]
    version = DocumentVersion(
        id=version_id, document_id=doc.id, version_no=version_no,
        file_path=str(save_path), file_hash=file_hash, file_size=len(content),
        created_by=user.id,
    )
    doc.current_version = version_no
    doc.current_version_id = version_id
    doc.file_path = str(save_path)
    doc.file_hash = file_hash
    doc.file_size = len(content)
    # 已发布旧版本继续在线；新版本的处理状态由 document_versions 独立维护。
    doc.status = "ready" if doc.published_version_id else "processing"
    doc.error_message = None
    doc.lock_version += 1
    db.add(version)
    from app.services.operations.audit import record_in_transaction
    record_in_transaction(db, user, "document.create_version", "document", doc.id,
                          detail={"kb_id": kb_id, "version": version_no})
    db.commit()
    enqueue_job("document_prepare", "document", doc.id, user,
                {"doc_id": doc.id}, f"document_prepare:{doc.id}:{version_no}")
    return UploadDocumentOut(doc_id=doc.id, filename=doc.filename, status="processing",
                             file_size=doc.file_size, kb_id=kb_id)


@router.get("/knowledge-bases/{kb_id}/documents", response_model=list[DocumentOut])
def list_kb_documents(
    kb_id: str, db: Session = Depends(get_db), user: User = Depends(require_content_manager)
) -> list[DocumentOut]:
    """知识库文档列表（含处理状态，前端轮询用）。"""
    require_kb_permission(db, kb_id, user, "write")
    docs = db.scalars(
        select(Document)
        .where(Document.kb_id == kb_id)
        .order_by(Document.created_at.desc())
    )
    docs = list(docs)
    topics = _topic_map(db, [doc.id for doc in docs])
    return [_document_out(db, d, topics.get(d.id)) for d in docs]


@router.get("/retrieval-topics")
def list_retrieval_topics(_user: User = Depends(require_content_manager)) -> list[dict]:
    """主题配置：管理端用于标注文档；配置异常时返回空列表而非接口失败。"""
    from app.core.retrieval.indexing.retrieval_topics import retrieval_topics

    return retrieval_topics.all()


@router.get("/knowledge-bases/{kb_id}/documents/{doc_id}/topics")
def get_document_topics(
    kb_id: str,
    doc_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_content_manager),
) -> dict:
    require_kb_permission(db, kb_id, user, "write")
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
    user: User = Depends(require_content_manager),
) -> dict:
    """实验室成员或管理员审核主题：选中的标签批准，其余 AI 待审建议驳回。"""
    require_kb_permission(db, kb_id, user, "write")
    doc = db.get(Document, doc_id)
    if doc is None or doc.kb_id != kb_id:
        raise NotFoundError("document_not_found", f"文档不存在：{doc_id}")
    from app.core.retrieval.indexing.retrieval_topics import retrieval_topics

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
            db.delete(row)  # 内容管理员取消此前已批准的标签
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
    kb_id: str, doc_id: str, db: Session = Depends(get_db), user: User = Depends(require_content_manager)
) -> dict:
    """归档文档：保留版本和审核历史；已发布文档只有管理员可归档。"""
    require_kb_permission(db, kb_id, user, "write")
    doc = db.get(Document, doc_id)
    if doc is None or doc.kb_id != kb_id:
        raise NotFoundError("document_not_found", f"文档不存在：{doc_id}")

    if doc.governance_status == "published":
        enforce_action(user, "document.archive")
    vector_store.delete_document(kb_id, doc_id)
    db.execute(delete(ChunkRecord).where(ChunkRecord.doc_id == doc_id))
    doc.governance_status = "archived"
    doc.deleted_at = utcnow()
    doc.lock_version += 1
    version = db.get(DocumentVersion, doc.current_version_id) if doc.current_version_id else None
    if version:
        version.review_status = "archived"
    from app.services.operations.audit import record_in_transaction
    record_in_transaction(db, user, "document.archive", "document", doc.id,
                          detail={"kb_id": kb_id})
    db.commit()
    # 关键词索引同步（缓存，异常不阻断）
    try:
        from app.core.retrieval.indexing.keyword_index import keyword_index

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
    user: User = Depends(require_content_manager),
) -> UploadDocumentOut:
    """[deprecated] 等价 POST /api/knowledge-bases/{kb}/documents（Phase 2 起处理后台化）。"""
    return await upload_document_to_kb(knowledge_base_id, background_tasks, file, db, user)


@router.get("/documents", response_model=list[DocumentOut])
def list_documents_deprecated(
    knowledge_base_id: str = KB_DEFAULT,
    db: Session = Depends(get_db),
    user: User = Depends(require_content_manager),
) -> list[DocumentOut]:
    """[deprecated] 等价 GET /api/knowledge-bases/{kb}/documents。"""
    return list_kb_documents(knowledge_base_id, db, user)


@router.get("/documents/{doc_id}/chunks")
def list_chunks(
    doc_id: str,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(require_content_manager),
) -> dict:
    """查看某文档的分块明细：内容、大小（字符数/token 估算）、位置元数据。"""
    doc = db.get(Document, doc_id)
    if doc is None:
        raise NotFoundError("document_not_found", f"文档不存在：{doc_id}")
    require_kb_permission(db, doc.kb_id, user, "write")
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


def _as_index_chunk(doc: Document, row: ChunkRecord, text: str | None = None) -> Chunk:
    return Chunk(
        doc_id=doc.id,
        kb_id=doc.kb_id,
        source_file=doc.filename,
        text=row.text if text is None else text,
        chunk_index=row.chunk_index,
        page=row.page,
        slide_number=row.slide_number,
        sheet_name=row.sheet_name,
        row_range=row.row_range,
        created_at=row.created_at.isoformat(),
    )


@router.patch("/documents/{doc_id}/chunks/{chunk_index}")
def update_chunk(
    doc_id: str,
    chunk_index: int,
    body: ChunkUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_content_manager),
) -> dict:
    """立即校订单个正式 chunk，并同步 SQL、向量索引和 BM25 缓存。

    这不会改写原始上传文件；从源文件重建索引会覆盖人工校订。
    """
    doc = db.get(Document, doc_id)
    if doc is None or doc.deleted_at is not None:
        raise NotFoundError("document_not_found", f"文档不存在：{doc_id}")
    require_kb_permission(db, doc.kb_id, user, "write")
    if doc.status != "ready":
        raise ConflictError("document_not_ready", "只能修改已处理完成的文档分块")

    new_text = body.text.strip()
    if not new_text:
        raise BadRequestError("empty_chunk", "chunk 内容不能为空")

    row = db.scalar(
        select(ChunkRecord)
        .where(ChunkRecord.doc_id == doc_id, ChunkRecord.chunk_index == chunk_index)
        .with_for_update()
    )
    if row is None:
        raise NotFoundError("chunk_not_found", f"chunk 不存在：{chunk_index}")
    expected = body.expected_updated_at.replace(tzinfo=None)
    if row.updated_at != expected:
        raise ConflictError("chunk_modified", "chunk 已被其他操作修改，请刷新后重试")
    if row.text == new_text:
        return {
            "chunk_index": row.chunk_index, "text": row.text,
            "char_length": row.char_length, "token_estimate": row.token_estimate,
            "page": row.page, "slide_number": row.slide_number,
            "sheet_name": row.sheet_name, "row_range": row.row_range,
            "created_at": row.created_at, "updated_at": row.updated_at,
        }

    all_rows = list(db.scalars(
        select(ChunkRecord).where(ChunkRecord.doc_id == doc_id).order_by(ChunkRecord.chunk_index)
    ))
    old_chunks = [_as_index_chunk(doc, item) for item in all_rows]
    new_chunks = [
        _as_index_chunk(doc, item, new_text if item.id == row.id else None)
        for item in all_rows
    ]
    old_chunk = _as_index_chunk(doc, row)
    new_chunk = _as_index_chunk(doc, row, new_text)

    # 先计算新旧向量；后续 SQL 提交若失败，可用旧向量补偿恢复。
    keyword_index = None
    vector_changed = False
    try:
        old_embedding, new_embedding = embed_texts([row.text, new_text])
        vector_store.upsert_chunk(doc.kb_id, new_chunk, new_embedding)
        vector_changed = True
        from app.core.retrieval.indexing.keyword_index import keyword_index

        keyword_index.add_document(doc.kb_id, doc.id, new_chunks)
    except Exception as exc:
        db.rollback()
        logger.exception("chunk 索引更新失败：doc=%s chunk=%s", doc_id, chunk_index)
        try:
            if vector_changed:
                vector_store.upsert_chunk(doc.kb_id, old_chunk, old_embedding)
            if keyword_index is not None:
                keyword_index.add_document(doc.kb_id, doc.id, old_chunks)
        except Exception:
            logger.exception("chunk 索引初始更新的补偿失败：doc=%s chunk=%s", doc_id, chunk_index)
        raise ApiError(502, "chunk_index_update_failed", "更新检索索引失败，原内容已保留") from exc

    old_text = row.text
    old_char_length = row.char_length
    old_token_estimate = row.token_estimate
    old_updated_at = row.updated_at
    version_row = None
    version = None
    if row.document_version_id:
        version = db.get(DocumentVersion, row.document_version_id)
        version_row = db.scalar(select(DocumentVersionChunk).where(
            DocumentVersionChunk.document_version_id == row.document_version_id,
            DocumentVersionChunk.chunk_index == row.chunk_index,
        ))
    try:
        row.text = new_text
        row.char_length = len(new_text)
        row.token_estimate = estimate_tokens(new_text)
        row.updated_at = utcnow()
        if version_row is not None:
            version_row.text = new_text
            version_row.char_length = row.char_length
            version_row.token_estimate = row.token_estimate
        if version is not None:
            version.content_hash = hashlib.sha256(
                "\n".join(chunk.text for chunk in new_chunks).encode("utf-8")
            ).hexdigest()
            version.change_summary = f"人工校订 chunk #{row.chunk_index}"
        doc.lock_version += 1
        from app.services.operations.audit import record_in_transaction

        record_in_transaction(
            db, user, "document.chunk_update", "document_chunk", row.id,
            detail={"doc_id": doc.id, "kb_id": doc.kb_id, "chunk_index": row.chunk_index},
        )
        db.commit()
        db.refresh(row)
    except Exception as exc:
        db.rollback()
        logger.exception("chunk 数据库更新失败，开始恢复索引：doc=%s chunk=%s", doc_id, chunk_index)
        try:
            vector_store.upsert_chunk(doc.kb_id, old_chunk, old_embedding)
            keyword_index.add_document(doc.kb_id, doc.id, old_chunks)
        except Exception:
            logger.exception("chunk 索引补偿失败：doc=%s chunk=%s", doc_id, chunk_index)
        # 局部变量用于明确补偿语义，同时防止未来改为手动 flush 时丢失旧值。
        row.text, row.char_length = old_text, old_char_length
        row.token_estimate, row.updated_at = old_token_estimate, old_updated_at
        raise ApiError(500, "chunk_update_failed", "chunk 保存失败，已尝试恢复原索引") from exc

    return {
        "chunk_index": row.chunk_index, "text": row.text,
        "char_length": row.char_length, "token_estimate": row.token_estimate,
        "page": row.page, "slide_number": row.slide_number,
        "sheet_name": row.sheet_name, "row_range": row.row_range,
        "created_at": row.created_at, "updated_at": row.updated_at,
    }
