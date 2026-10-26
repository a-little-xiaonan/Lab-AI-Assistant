"""把持久化回答依据投影为确定性的自然语言说明，不让模型猜来源。"""
from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.database import AnswerProvenance, Document, Message
from app.observability.tracing import stage

MAX_DERIVATION_DEPTH = 3


@dataclass(frozen=True)
class SourceExplanation:
    text: str
    answer_kind: str
    basis_status: str
    derived_from_message_ids: tuple[int, ...] = ()
    permission_projected: bool = False


def load_provenance(db: Session, assistant_message_id: int) -> AnswerProvenance | None:
    with stage("provenance.load", target_message_ids=[assistant_message_id]) as observed:
        row = db.get(AnswerProvenance, assistant_message_id)
        observed.set_attributes(
            provenance_exists=row is not None,
            answer_kind=row.answer_kind if row else None,
            basis_status=row.basis_status if row else None,
        )
        return row


def _json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _authorized_documents(db: Session, refs: list[dict], readable_kb_ids: set[str]) -> list[str]:
    """只返回当前仍有知识库读取权限且未删除的文档名称。"""
    doc_ids = []
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("type") != "knowledge":
            continue
        kb_id = ref.get("knowledge_base_id")
        doc_id = ref.get("doc_id")
        if isinstance(kb_id, str) and kb_id in readable_kb_ids and isinstance(doc_id, str):
            doc_ids.append(doc_id)
    if not doc_ids:
        return []
    rows = db.scalars(
        select(Document).where(
            Document.id.in_(list(dict.fromkeys(doc_ids))[:20]),
            Document.deleted_at.is_(None),
        )
    )
    names = []
    for document in rows:
        if document.kb_id in readable_kb_ids and document.filename not in names:
            names.append(document.filename[:120])
    return names


def _knowledge_phrase(db: Session, provenance: AnswerProvenance, readable_kb_ids: set[str]) -> tuple[str, bool]:
    refs = _json_list(provenance.evidence_refs_json)
    knowledge_count = sum(
        1 for ref in refs if isinstance(ref, dict) and ref.get("type") == "knowledge"
    )
    if not knowledge_count:
        return "记录了知识库来源类型，但没有可展示的文档引用。", False
    names = _authorized_documents(db, refs, readable_kb_ids)
    if names:
        shown = "、".join(names[:5])
        return (
            f"依据知识库检索得到的资料生成，提供的资料涉及：{shown}。"
            "这些资料只能说明当时提供给生成阶段的参考，不能证明回答中的每句话都被逐句核实。",
            True,
        )
    return (
        "依据知识库检索得到的资料生成；当前无法展示具体文档名称。"
        "这些资料只表示当时提供过参考，不能证明回答中的每句话都被逐句核实。",
        False,
    )


def _tool_names(provenance: AnswerProvenance) -> list[str]:
    refs = _json_list(provenance.evidence_refs_json)
    names = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        kind = ref.get("type")
        if isinstance(kind, str) and kind != "knowledge" and kind not in names:
            names.append(kind[:32])
    return names[:5]


def _explain_loaded(
    db: Session,
    provenance: AnswerProvenance,
    readable_kb_ids: set[str],
    depth: int,
    seen: set[int],
    session_id: str | None = None,
) -> SourceExplanation:
    kind = provenance.answer_kind or "unknown"
    status = provenance.basis_status or "unknown"
    derived = tuple(
        item for item in _json_list(provenance.derived_from_message_ids_json)
        if isinstance(item, int)
    )

    if status == "unknown" or kind in {"unknown", "unavailable"}:
        return SourceExplanation(
            text="这条历史回答没有保存可确认的来源记录，无法确认当时是否使用了知识库或工具。",
            answer_kind=kind,
            basis_status="unknown" if status == "unknown" else status,
            derived_from_message_ids=derived,
        )
    if kind == "general":
        return SourceExplanation(
            text="刚才那条回答是模型给出的一般性建议，当时没有查询知识库，也没有使用实时工具。",
            answer_kind=kind,
            basis_status=status,
            derived_from_message_ids=derived,
        )
    if kind == "knowledge":
        phrase, projected = _knowledge_phrase(db, provenance, readable_kb_ids)
        return SourceExplanation(
            text=f"刚才那条回答{phrase}",
            answer_kind=kind,
            basis_status=status,
            derived_from_message_ids=derived,
            permission_projected=projected,
        )
    if kind == "tool":
        tools = _tool_names(provenance)
        detail = f"使用了实时工具：{'、'.join(tools)}。" if tools else "使用了实时工具查询。"
        return SourceExplanation(
            text=f"刚才那条回答{detail}工具结果只证明当次查询返回的内容。",
            answer_kind=kind,
            basis_status=status,
            derived_from_message_ids=derived,
        )
    if kind == "mixed":
        tools = _tool_names(provenance)
        detail = f"其中工具部分使用了：{'、'.join(tools)}。" if tools else ""
        return SourceExplanation(
            text="刚才那条回答由通用建议、知识资料或工具结果中的多个部分组成。" + detail,
            answer_kind=kind,
            basis_status=status,
            derived_from_message_ids=derived,
        )
    if kind == "transformed":
        if not derived:
            return SourceExplanation(
                text="这条历史回答是改写结果，但没有保存被改写的原始回答，无法继续确认来源。",
                answer_kind=kind,
                basis_status="unknown",
            )
        if depth >= MAX_DERIVATION_DEPTH:
            return SourceExplanation(
                text="这条历史回答由更早的回答改写而来，但派生链过深，无法继续确认来源。",
                answer_kind=kind,
                basis_status="unknown",
                derived_from_message_ids=derived,
            )
        source = next((item for item in derived if item not in seen), None)
        if source is None:
            return SourceExplanation(
                text="这条历史回答的派生关系存在循环或断链，无法继续确认来源。",
                answer_kind=kind,
                basis_status="unknown",
                derived_from_message_ids=derived,
            )
        target = db.get(Message, source)
        if (target is None or target.role != "assistant"
                or (session_id is not None and target.session_id != session_id)):
            return SourceExplanation(
                text="这条历史回答由更早的回答改写而来，但找不到原始回答，无法确认来源。",
                answer_kind=kind,
                basis_status="unknown",
                derived_from_message_ids=derived,
            )
        parent = load_provenance(db, source)
        if parent is None:
            return SourceExplanation(
                text="这条历史回答由更早的回答改写而来，但原始回答没有来源记录，无法确认来源。",
                answer_kind=kind,
                basis_status="unknown",
                derived_from_message_ids=derived,
            )
        nested = _explain_loaded(
            db, parent, readable_kb_ids, depth + 1, seen | {source}, session_id
        )
        return SourceExplanation(
            text="这条历史回答由更早的回答改写而来，没有重新核验事实；"
                 f"该原始回答的来源情况是：{nested.text}",
            answer_kind=kind,
            basis_status=nested.basis_status,
            derived_from_message_ids=derived,
            permission_projected=nested.permission_projected,
        )
    if kind == "clarification":
        return SourceExplanation(
            text="上一条回答是在请求补充必要信息，当时没有取得知识库或工具依据。",
            answer_kind=kind,
            basis_status=status,
            derived_from_message_ids=derived,
        )
    if kind == "provenance":
        return SourceExplanation(
            text="上一条回答是在说明更早回答的来源记录，没有重新检索或核验当时的事实。",
            answer_kind=kind,
            basis_status=status,
            derived_from_message_ids=derived,
        )
    return SourceExplanation(
        text="这条历史回答记录了其他来源类型，但没有可进一步说明的安全字段。",
        answer_kind=kind,
        basis_status=status,
        derived_from_message_ids=derived,
    )


def explain_source(
    db: Session,
    target: Message,
    readable_kb_ids: list[str],
) -> SourceExplanation:
    """读取目标回答当时的依据并做权限投影；不重新检索。"""
    with stage(
        "provenance.project",
        target_message_ids=[target.id],
    ) as observed:
        provenance = load_provenance(db, target.id)
        if provenance is None:
            explanation = SourceExplanation(
                text="这条历史回答没有保存来源记录，无法确认当时是否使用了知识库或工具。",
                answer_kind="unknown",
                basis_status="unknown",
            )
        else:
            explanation = _explain_loaded(
                db, provenance, set(readable_kb_ids), 0, {target.id}, target.session_id
            )
        observed.set_attributes(
            provenance_exists=provenance is not None,
            answer_kind=explanation.answer_kind,
            basis_status=explanation.basis_status,
            derived_from_message_ids=list(explanation.derived_from_message_ids),
            derivation_depth=len(explanation.derived_from_message_ids),
            permission_projected=explanation.permission_projected,
        )
        return explanation
