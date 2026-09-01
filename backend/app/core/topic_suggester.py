"""文档主题 AI 初标：只写 pending 建议，管理员审核前绝不参与定向检索。"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.retrieval_topics import retrieval_topics
from app.llm import qwen
from app.llm.prompt_templates import build_document_topic_messages
from app.models.database import Document, DocumentTopic

logger = logging.getLogger(__name__)


def _parse(raw: str) -> list[tuple[str, float | None]]:
    """容错提取 JSON 数组；无效输出直接视为空建议。"""
    match = re.search(r"\[[\s\S]*\]", raw)
    if not match:
        return []
    try:
        values = json.loads(match.group(0))
    except (TypeError, ValueError):
        return []
    if not isinstance(values, list):
        return []
    result: list[tuple[str, float | None]] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip()
        if not code or code in seen:
            continue
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = None
        seen.add(code)
        result.append((code, confidence))
    return result[: settings.ai_topic_label_max]


def suggest_topics(db: Session, doc: Document, chunks: list) -> list[str]:
    """为已解析文档写入待审核主题。失败仅记录日志，不影响入库成功状态。"""
    if not settings.ai_topic_labeling_enabled:
        return []
    topics = retrieval_topics.all()
    if not topics or not chunks:
        return []
    text = "\n".join(chunk.text for chunk in chunks[:12])
    try:
        raw = qwen.chat_completion(build_document_topic_messages(text, topics))
        parsed = _parse(raw)
        valid = retrieval_topics.valid_codes([code for code, _ in parsed])
        confidence_by_code = dict(parsed)
        existing_codes = set(
            db.scalars(
                select(DocumentTopic.topic_code).where(
                    DocumentTopic.doc_id == doc.id,
                )
            )
        )
        valid = [code for code in valid if code not in existing_codes]
        # 仅替换此前 AI 待审核建议，不影响管理员已审核的标签。
        db.execute(
            delete(DocumentTopic).where(
                DocumentTopic.doc_id == doc.id,
                DocumentTopic.source == "ai_suggested",
                DocumentTopic.review_status == "pending",
            )
        )
        db.add_all(
            DocumentTopic(
                doc_id=doc.id,
                topic_code=code,
                source="ai_suggested",
                confidence=confidence_by_code.get(code),
                review_status="pending",
            )
            for code in valid
        )
        db.commit()
        logger.info("AI 主题初标完成：doc=%s suggestions=%d", doc.id, len(valid))
        return valid
    except Exception:
        db.rollback()
        logger.exception("AI 主题初标失败，文档仍保持可用：doc=%s", doc.id)
        return []
