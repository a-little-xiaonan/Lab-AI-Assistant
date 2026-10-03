"""公开 FAQ 模板：只在公开、已发布且快照一致时命中。"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from app.core.faq.service import _snapshot, match_public_faq, normalize_query
from app.llm.prompt_templates import build_faq_stable_prefix
from app.models.database import (
    ChunkRecord,
    Document,
    DocumentVersion,
    FaqTemplate,
    FaqTemplateAlias,
    FaqTemplateEvidence,
    KnowledgeBase,
)
from scripts.knowledge import import_faq_template


def _seed_active_template(db):
    kb = KnowledgeBase(id="kb_public", name="公开资料", access_level="guest", status="active")
    document = Document(
        id="doc_recruit", kb_id=kb.id, filename="招新公告.md", file_hash="file_hash",
        file_path="/tmp/recruit.md", status="ready", governance_status="published",
    )
    version = DocumentVersion(
        id="ver_recruit_1", document_id=document.id, version_no=1,
        file_path="/tmp/recruit.md", file_hash="file_hash", content_hash="content_hash",
        processing_status="ready", review_status="published",
    )
    document.published_version_id = version.id
    chunk = ChunkRecord(
        id="doc_recruit_0", doc_id=document.id, document_version_id=version.id,
        kb_id=kb.id, chunk_index=0, text="本期招新面向全校本科生，报名截止时间为 10 月 20 日。",
    )
    template = FaqTemplate(
        id="faq_recruit_deadline", code="recruit_deadline", title="招新报名截止时间",
        kb_scope_json=json.dumps([kb.id]), status="active", knowledge_snapshot="pending",
        stable_prompt_hash="pending",
    )
    evidence = FaqTemplateEvidence(
        id="faq_evidence_1", template_id=template.id, doc_id=document.id,
        document_version_id=version.id, chunk_index=0, excerpt=chunk.text,
        citation_label="招新公告：报名时间", sort_order=1,
    )
    alias = FaqTemplateAlias(
        id="faq_alias_1", template_id=template.id,
        normalized_query=normalize_query("报名截止时间是什么时候？"), match_mode="exact",
    )
    db.add_all([kb, document, version, chunk, template, evidence, alias])
    db.flush()
    template.knowledge_snapshot = _snapshot([(evidence, document, version)])
    prefix = build_faq_stable_prefix(
        template.code, template.template_version, template.knowledge_snapshot,
        [(evidence.citation_label, evidence.excerpt)],
    )
    template.stable_prompt_hash = hashlib.sha256(prefix.encode()).hexdigest()
    db.commit()
    return template, document


def test_match_public_faq_returns_verified_evidence(db_session):
    template, _document = _seed_active_template(db_session)

    matched = match_public_faq("报名截止时间是什么时候？", ["kb_public"], db=db_session)

    assert matched is not None
    assert matched.code == template.code
    assert matched.chunks[0].text.startswith("本期招新面向")
    assert "FAQ 模板：recruit_deadline:v1" in matched.stable_prefix


def test_match_public_faq_rejects_scope_or_published_version_mismatch(db_session):
    _template, document = _seed_active_template(db_session)

    assert match_public_faq("报名截止时间是什么时候？", ["kb_private"], db=db_session) is None

    document.published_version_id = "ver_recruit_2"
    db_session.commit()
    assert match_public_faq("报名截止时间是什么时候？", ["kb_public"], db=db_session) is None


def test_import_script_creates_only_validated_active_template(db_session, monkeypatch):
    """管理员清单须绑定当前公开发布版本，导入后才能被 FAQ 路由使用。"""
    kb = KnowledgeBase(id="kb_import", name="导入公开资料", access_level="guest", status="active")
    document = Document(
        id="doc_import", kb_id=kb.id, filename="公告.md", file_hash="file_hash",
        file_path="/tmp/import.md", status="ready", governance_status="published",
    )
    version = DocumentVersion(
        id="ver_import_1", document_id=document.id, version_no=1,
        file_path="/tmp/import.md", file_hash="file_hash", content_hash="content_hash",
    )
    document.published_version_id = version.id
    chunk = ChunkRecord(
        id="doc_import_0", doc_id=document.id, document_version_id=version.id,
        kb_id=kb.id, chunk_index=0, text="招新报名截止时间为 10 月 20 日。",
    )
    db_session.add_all([kb, document, version, chunk])
    db_session.commit()
    monkeypatch.setattr(import_faq_template, "SessionLocal", lambda: db_session)
    manifest = {
        "code": "recruit_deadline", "title": "报名截止时间", "kb_scope": [kb.id],
        "aliases": [{"query": "报名截止时间是什么时候？", "priority": 10}],
        "evidences": [{
            "doc_id": document.id, "document_version_id": version.id, "chunk_index": 0,
            "excerpt": "招新报名截止时间为 10 月 20 日。", "citation_label": "公告：报名时间",
        }],
    }

    assert import_faq_template.import_template(manifest, activate=True) == "recruit_deadline"
    template = db_session.scalar(select(FaqTemplate).where(FaqTemplate.code == "recruit_deadline"))
    assert template is not None and template.status == "active"
    assert match_public_faq("报名截止时间是什么时候？", [kb.id], db=db_session) is not None
