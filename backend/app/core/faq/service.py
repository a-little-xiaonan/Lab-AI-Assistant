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


def _active_evidence(db, template: FaqTemplate, readable_kb_ids: set[str]):
    evidence_rows = list(db.scalars(select(FaqTemplateEvidence).where(
        FaqTemplateEvidence.template_id == template.id
    ).order_by(FaqTemplateEvidence.sort_order, FaqTemplateEvidence.id)))
    if not evidence_rows:
        return None
    scope = _scope(template.kb_scope_json)
    if not scope or not scope.issubset(readable_kb_ids):
        return None
    now: datetime = utcnow()
    checked: list[tuple[FaqTemplateEvidence, Document, DocumentVersion, ChunkRecord]] = []
    for evidence in evidence_rows:
        document = db.get(Document, evidence.doc_id)
        if document is None or document.kb_id not in scope or document.kb_id not in readable_kb_ids:
            return None
        kb = db.get(KnowledgeBase, document.kb_id)
        if kb is None or kb.access_level != "guest" or kb.status != "active":
            return None
        if (document.status != "ready" or document.governance_status != "published"
                or document.deleted_at is not None or document.published_version_id != evidence.document_version_id
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
    return checked


def match_public_faq(query: str, readable_kb_ids: list[str], *, db=None) -> FaqMatch | None:
    """匹配公开模板并验证所有绑定证据；不确定时返回 None 回退 RAG。"""
    normalized = normalize_query(query)
    if not normalized or not readable_kb_ids:
        return None
    readable = set(readable_kb_ids)
    owns_session = db is None
    if owns_session:
        db = SessionLocal()
    try:
        aliases = list(db.scalars(select(FaqTemplateAlias).join(
            FaqTemplate, FaqTemplate.id == FaqTemplateAlias.template_id
        ).where(
            FaqTemplate.status == "active", FaqTemplate.audience_scope == "public"
        ).order_by(FaqTemplateAlias.priority.desc(), FaqTemplateAlias.id)))
        for alias in aliases:
            if not _alias_matches(alias, normalized):
                continue
            template = db.get(FaqTemplate, alias.template_id)
            if template is None:
                continue
            checked = _active_evidence(db, template, readable)
            if checked is None:
                continue
            snapshot_rows = [(evidence, document, version) for evidence, document, version, _ in checked]
            if _snapshot(snapshot_rows) != template.knowledge_snapshot:
                continue
            cards = [(evidence.citation_label, evidence.excerpt) for evidence, _, _, _ in checked]
            stable_prefix = build_faq_stable_prefix(
                template.code, template.template_version, template.knowledge_snapshot, cards
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
            return FaqMatch(template.code, stable_prefix, chunks)
        return None
    finally:
        if owns_session:
            db.close()
