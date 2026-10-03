"""从人工审核的 JSON 清单创建公开 FAQ 模板。

默认创建 draft，确认全部证据均为当前公开发布版本后才可传 --activate。
不更新已有 code，避免误把运行中的模板覆盖为未经审核的内容。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/

from app.core.faq.service import _snapshot, normalize_query
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

CODE_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


def _load_manifest(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 JSON 清单：{exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("清单根节点必须是 JSON 对象")
    return raw


def _required_string(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 必须是非空字符串")
    return value.strip()


def _normalized_alias(item: dict) -> tuple[str, str, int]:
    query = _required_string(item, "query")
    mode = item.get("match_mode", "exact")
    if mode not in {"exact", "keywords"}:
        raise ValueError("alias.match_mode 只能是 exact 或 keywords")
    if mode == "exact":
        normalized = normalize_query(query)
    else:
        normalized = "|".join(normalize_query(part) for part in query.split("|") if normalize_query(part))
    if not normalized:
        raise ValueError("alias.query 规范化后不能为空")
    priority = item.get("priority", 0)
    if not isinstance(priority, int):
        raise ValueError("alias.priority 必须是整数")
    return normalized, mode, priority


def import_template(manifest: dict, *, activate: bool) -> str:
    code = _required_string(manifest, "code")
    title = _required_string(manifest, "title")
    if not CODE_RE.fullmatch(code):
        raise ValueError("code 需为 3-64 位小写字母、数字或下划线，且以字母开头")
    scope = manifest.get("kb_scope")
    aliases = manifest.get("aliases")
    evidences = manifest.get("evidences")
    if not isinstance(scope, list) or not scope or not all(isinstance(kb_id, str) and kb_id for kb_id in scope):
        raise ValueError("kb_scope 必须是非空知识库 ID 列表")
    if not isinstance(aliases, list) or not aliases or not all(isinstance(item, dict) for item in aliases):
        raise ValueError("aliases 必须是非空对象列表")
    if not isinstance(evidences, list) or not evidences or not all(isinstance(item, dict) for item in evidences):
        raise ValueError("evidences 必须是非空对象列表")
    scope_set = set(scope)
    normalized_aliases = [_normalized_alias(item) for item in aliases]
    if len({item[0] for item in normalized_aliases}) != len(normalized_aliases):
        raise ValueError("同一模板内不允许重复的规范化别名")

    with SessionLocal() as db:
        if db.scalar(select(FaqTemplate.id).where(FaqTemplate.code == code)):
            raise ValueError(f"模板 code 已存在：{code}；P1 不允许脚本覆盖已有模板")
        kbs = {kb.id: kb for kb in db.scalars(select(KnowledgeBase).where(KnowledgeBase.id.in_(scope_set)))}
        if set(kbs) != scope_set or any(kb.access_level != "guest" or kb.status != "active" for kb in kbs.values()):
            raise ValueError("kb_scope 必须全部是 active 的 guest 公开知识库")

        now = utcnow()
        template = FaqTemplate(
            id=f"faq_{uuid4().hex[:24]}", code=code, title=title,
            kb_scope_json=json.dumps(scope, ensure_ascii=False), audience_scope="public",
            template_version=1, prompt_policy_version="faq-policy-v1", status="draft",
            knowledge_snapshot="pending", stable_prompt_hash="pending",
        )
        db.add(template)
        checked = []
        cards = []
        for order, item in enumerate(evidences, 1):
            doc_id = _required_string(item, "doc_id")
            version_id = _required_string(item, "document_version_id")
            excerpt = _required_string(item, "excerpt")
            label = _required_string(item, "citation_label")
            chunk_index = item.get("chunk_index")
            if not isinstance(chunk_index, int) or chunk_index < 0:
                raise ValueError("evidence.chunk_index 必须是非负整数")
            document = db.get(Document, doc_id)
            version = db.get(DocumentVersion, version_id)
            chunk = db.scalar(select(ChunkRecord).where(
                ChunkRecord.doc_id == doc_id,
                ChunkRecord.document_version_id == version_id,
                ChunkRecord.chunk_index == chunk_index,
            ))
            if (document is None or version is None or chunk is None or document.kb_id not in scope_set
                    or document.status != "ready" or document.governance_status != "published"
                    or document.published_version_id != version_id or version.document_id != doc_id
                    or document.deleted_at is not None
                    or (document.effective_at is not None and document.effective_at > now)
                    or (document.expires_at is not None and document.expires_at <= now)
                    or excerpt not in chunk.text):
                raise ValueError(f"证据 {order} 不是当前已发布公开 Chunk 的受控摘录")
            evidence = FaqTemplateEvidence(
                id=f"faqev_{uuid4().hex[:22]}", template_id=template.id, doc_id=doc_id,
                document_version_id=version_id, chunk_index=chunk_index, excerpt=excerpt,
                citation_label=label, sort_order=item.get("sort_order", order),
            )
            if not isinstance(evidence.sort_order, int):
                raise ValueError("evidence.sort_order 必须是整数")
            db.add(evidence)
            checked.append((evidence, document, version))
            cards.append((label, excerpt))
        template.knowledge_snapshot = _snapshot(checked)
        prefix = build_faq_stable_prefix(code, template.template_version, template.knowledge_snapshot, cards)
        template.stable_prompt_hash = hashlib.sha256(prefix.encode()).hexdigest()
        for normalized, mode, priority in normalized_aliases:
            db.add(FaqTemplateAlias(
                id=f"faqal_{uuid4().hex[:22]}", template_id=template.id,
                normalized_query=normalized, match_mode=mode, priority=priority,
            ))
        if activate:
            template.status = "active"
        db.commit()
    return code


def main() -> None:
    parser = argparse.ArgumentParser(description="导入公开 FAQ 模板（默认草稿）")
    parser.add_argument("manifest", type=Path, help="人工审核后的 JSON 清单路径")
    parser.add_argument("--activate", action="store_true", help="校验通过后直接激活模板")
    args = parser.parse_args()
    code = import_template(_load_manifest(args.manifest), activate=args.activate)
    print(f"FAQ 模板已创建：{code}（状态：{'active' if args.activate else 'draft'}）")


if __name__ == "__main__":
    main()
