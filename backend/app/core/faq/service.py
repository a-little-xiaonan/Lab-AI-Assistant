"""公开 FAQ 模板匹配与运行时证据校验。

模板路径只读取已激活的 public 模板；任何范围、版本或快照不一致均返回空，
由调用方回退常规 RAG。此处不写模板状态，避免问答热路径产生额外事务。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from app.authorization.policy import LEVEL_VALUE
from app.config import settings
from app.core.retrieval.retriever import RetrievedChunk
from app.llm.prompt_templates import build_faq_stable_prefix
from app.models.database import (
    ChunkRecord,
    Document,
    DocumentVersion,
    FaqTemplate,
    FaqTemplateAlias,
    FaqTemplateEvidence,
    KnowledgeBase,
    utcnow,
)
from app.store.db import SessionLocal


@dataclass(frozen=True)
class FaqMatch:
    code: str
    audience_scope: str
    cache_namespace: str
    stable_prefix: str
    chunks: list[RetrievedChunk]


def normalize_query(query: str) -> str:
    """保守规范化：仅统一格式，不猜测语义，防止宽泛问题误命中模板。"""
    text = query.casefold().strip()
    text = re.sub(r"[？?！!。,.，、：:；;（）()【】\[\]\\s]+", "", text)
    return text


def _scope(value: str) -> set[str] | None:
    try:
        items = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(items, list) or not all(isinstance(item, str) and item for item in items):
        return None
    return set(items)


def _alias_matches(alias: FaqTemplateAlias, normalized_query: str) -> bool:
    if alias.match_mode == "exact":
        return alias.normalized_query == normalized_query
    if alias.match_mode == "keywords":
        parts = [part for part in alias.normalized_query.split("|") if part]
        return bool(parts) and all(part in normalized_query for part in parts)
    return False


def _snapshot(rows: list[tuple[FaqTemplateEvidence, Document, DocumentVersion]]) -> str:
    data = [
        (evidence.doc_id, version.id, version.content_hash or document.file_hash,
         evidence.chunk_index, hashlib.sha256(evidence.excerpt.encode()).hexdigest())
        for evidence, document, version in rows
    ]
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def cache_namespace(template: FaqTemplate, kbs: dict[str, KnowledgeBase]) -> str:
    """计算不含用户身份的缓存命名空间；变化即产生不同稳定前缀。"""
    scope = _scope(template.kb_scope_json) or set()
    payload = {
        "model_id": settings.context_cache_model or settings.llm_model,
        "provider_protocol_version": settings.context_cache_protocol_version,
        "template_code": template.code,
        "template_version": template.template_version,
        "prompt_policy_version": template.prompt_policy_version,
        "audience_scope": template.audience_scope,
        "template_kb_scope_and_level": [
            {"id": kb_id, "access_level": kbs[kb_id].access_level}
            for kb_id in sorted(scope) if kb_id in kbs
        ],
        "knowledge_snapshot": template.knowledge_snapshot,
        "tool_schema_version": settings.faq_tool_schema_version,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _active_evidence(
    db, template: FaqTemplate, readable_kb_ids: set[str], user_access_level: str
):
    evidence_rows = list(db.scalars(select(FaqTemplateEvidence).where(
        FaqTemplateEvidence.template_id == template.id
    ).order_by(FaqTemplateEvidence.sort_order, FaqTemplateEvidence.id)))
    if not evidence_rows:
        return None
    scope = _scope(template.kb_scope_json)
    if (not scope or not scope.issubset(readable_kb_ids)
            or template.audience_scope not in LEVEL_VALUE
            or user_access_level not in LEVEL_VALUE
            or LEVEL_VALUE[template.audience_scope] > LEVEL_VALUE[user_access_level]
            or template.prompt_policy_version != settings.faq_prompt_policy_version):
        return None
    scope_kbs: dict[str, KnowledgeBase] = {}
    for kb_id in scope:
        kb = db.get(KnowledgeBase, kb_id)
        if (kb is None or kb.status != "active" or kb.access_level != template.audience_scope):
            return None
        scope_kbs[kb_id] = kb
    now: datetime = utcnow()
    checked: list[tuple[FaqTemplateEvidence, Document, DocumentVersion, ChunkRecord]] = []
    for evidence in evidence_rows:
        document = db.get(Document, evidence.doc_id)
        if document is None or document.kb_id not in scope or document.kb_id not in readable_kb_ids:
            return None
        if (document.status != "ready" or document.governance_status != "published"
                or document.deleted_at is not None or document.published_version_id != evidence.document_version_id
                or document.sensitivity_level != template.audience_scope
                or (document.effective_at is not None and document.effective_at > now)
                or (document.expires_at is not None and document.expires_at <= now)):
            return None
        version = db.get(DocumentVersion, evidence.document_version_id)
        chunk = db.scalar(select(ChunkRecord).where(
            ChunkRecord.doc_id == document.id,
            ChunkRecord.document_version_id == evidence.document_version_id,
            ChunkRecord.chunk_index == evidence.chunk_index,
        ))
        if (version is None or version.document_id != document.id or chunk is None
                or chunk.kb_id != document.kb_id):
            return None
        checked.append((evidence, document, version, chunk))
    return checked, scope_kbs


def match_faq(
    query: str, readable_kb_ids: list[str], user_access_level: str, *, db=None
) -> FaqMatch | None:
    """匹配用户可访问的模板并验证证据；不确定时返回 None 回退 RAG。"""
    normalized = normalize_query(query)
    if not normalized or not readable_kb_ids or user_access_level not in LEVEL_VALUE:
        return None
    readable = set(readable_kb_ids)
    owns_session = db is None
    if owns_session:
        db = SessionLocal()
    try:
        aliases = list(db.scalars(select(FaqTemplateAlias).join(
            FaqTemplate, FaqTemplate.id == FaqTemplateAlias.template_id
        ).where(
            FaqTemplate.status == "active"
        ).order_by(FaqTemplateAlias.priority.desc(), FaqTemplateAlias.id)))
        for alias in aliases:
            if not _alias_matches(alias, normalized):
                continue
            template = db.get(FaqTemplate, alias.template_id)
            if template is None:
                continue
            validation = _active_evidence(db, template, readable, user_access_level)
            if validation is None:
                continue
            checked, scope_kbs = validation
            snapshot_rows = [(evidence, document, version) for evidence, document, version, _ in checked]
            if _snapshot(snapshot_rows) != template.knowledge_snapshot:
                continue
            cards = [(evidence.citation_label, evidence.excerpt) for evidence, _, _, _ in checked]
            namespace = cache_namespace(template, scope_kbs)
            stable_prefix = build_faq_stable_prefix(
                template.code, template.template_version, template.knowledge_snapshot,
                template.audience_scope, namespace, cards,
            )
            if hashlib.sha256(stable_prefix.encode()).hexdigest() != template.stable_prompt_hash:
                continue
            chunks = [
                RetrievedChunk(
                    chunk_id=chunk.id,
                    text=evidence.excerpt,
                    score=1.0,
                    metadata={
                        "doc_id": document.id,
                        "kb_id": document.kb_id,
                        "source_file": document.filename,
                        "chunk_index": chunk.chunk_index,
                        "page": chunk.page,
                        "slide_number": chunk.slide_number,
                        "sheet_name": chunk.sheet_name,
                        "row_range": chunk.row_range,
                    },
                )
                for evidence, document, _version, chunk in checked
            ]
            return FaqMatch(template.code, template.audience_scope, namespace, stable_prefix, chunks)
        return None
    finally:
        if owns_session:
            db.close()


def match_public_faq(query: str, readable_kb_ids: list[str], *, db=None) -> FaqMatch | None:
    """P1 兼容入口：访客只能命中 guest 模板。"""
    return match_faq(query, readable_kb_ids, "guest", db=db)


def mark_templates_stale_for_document(db, doc_id: str) -> list[str]:
    """标记引用指定文档的活跃模板；由调用方同事务记录审计并提交。"""
    rows = list(db.scalars(select(FaqTemplate).join(
        FaqTemplateEvidence, FaqTemplateEvidence.template_id == FaqTemplate.id
    ).where(FaqTemplateEvidence.doc_id == doc_id, FaqTemplate.status == "active").distinct()))
    for template in rows:
        template.status = "stale"
        template.updated_at = utcnow()
    return [template.id for template in rows]


def mark_templates_stale_for_kb(db, kb_id: str) -> list[str]:
    """标记作用域包含指定知识库的活跃模板，兼容 JSON 作用域的多数据库实现。"""
    rows = list(db.scalars(select(FaqTemplate).where(FaqTemplate.status == "active")))
    stale = [template for template in rows if kb_id in (_scope(template.kb_scope_json) or set())]
    for template in stale:
        template.status = "stale"
        template.updated_at = utcnow()
    return [template.id for template in stale]
