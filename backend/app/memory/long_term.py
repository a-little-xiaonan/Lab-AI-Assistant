"""用户级长期记忆（Phase 4-02）。

每条记忆必须绑定 user_id；session_id 只记录来源，kb_id 只记录产生语境。
旧版按 kb_{kb_id}_memory 共享召回的实现不再用于聊天链路，避免新生之间串用偏好。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from uuid import uuid4

from sqlalchemy import select

from app.config import settings
from app.llm import qwen
from app.llm.prompt_templates import build_memory_extract_messages
from app.models.database import UserMemory
from app.store.db import SessionLocal
from app.store.vector_store import vector_store

logger = logging.getLogger(__name__)

MEMORY_TYPES = {
    "profile": "个人背景",
    "preference": "用户偏好",
    "fact": "关键事实",
    "goal": "目标",
}
MEMORY_CONTENT_MAX = 500


def _parse_extraction(text: str) -> list[dict]:
    """容错解析 LLM 输出的 JSON 数组。"""
    cleaned = text.strip()
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.S)
    if match:
        cleaned = match.group(1).strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("未找到 JSON 数组")
    items = json.loads(cleaned[start : end + 1])
    if not isinstance(items, list):
        raise ValueError("JSON 不是数组")
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("content"):
            continue
        kind = str(item.get("type", "fact"))
        if kind == "user_preference":
            kind = "preference"
        elif kind in {"key_fact", "faq_pair", "entity"}:
            kind = "fact"
        elif kind not in MEMORY_TYPES:
            kind = "fact"
        out.append({
            "type": kind,
            "content": str(item["content"])[:MEMORY_CONTENT_MAX],
            "confidence": float(item.get("confidence", 0)),
        })
    return out


class LongTermMemory:
    def extract_and_store(
        self, user_id: str, session_id: str, kb_id: str, messages: list[tuple[str, str]]
    ) -> int:
        """后台提取并写入用户自己的记忆；失败绝不阻塞聊天主链路。"""
        try:
            text = qwen.chat_completion(build_memory_extract_messages(messages))
            items = _parse_extraction(text)
        except Exception:
            logger.exception("记忆提取失败（user=%s，session=%s）", user_id, session_id)
            return 0
        kept = [item for item in items if item["confidence"] > settings.memory_confidence_threshold]
        if not kept:
            return 0
        try:
            embeddings = qwen.embed_texts([item["content"] for item in kept])
        except Exception:
            logger.exception("记忆向量化失败（user=%s，session=%s）", user_id, session_id)
            return 0

        entries: list[dict] = []
        db = SessionLocal()
        try:
            for item, embedding in zip(kept, embeddings):
                content_hash = hashlib.sha1(item["content"].encode("utf-8")).hexdigest()
                row = db.scalar(select(UserMemory).where(
                    UserMemory.user_id == user_id,
                    UserMemory.content_hash == content_hash,
                ))
                if row is None:
                    row = UserMemory(
                        id=f"mem_{uuid4().hex[:16]}",
                        user_id=user_id,
                        memory_type=item["type"],
                        content=item["content"],
                        content_hash=content_hash,
                        confidence=item["confidence"],
                        source_session_id=session_id,
                        scope_kb_id=kb_id,
                    )
                    db.add(row)
                    db.flush()
                else:
                    row.memory_type = item["type"]
                    row.content = item["content"]
                    row.confidence = item["confidence"]
                    row.source_session_id = session_id
                    row.scope_kb_id = kb_id
                    row.status = "active"
                entries.append({
                    "id": row.id,
                    "content": item["content"],
                    "embedding": embedding,
                    "metadata": {
                        "user_id": user_id,
                        "memory_type": item["type"],
                        "source_session_id": session_id,
                        "status": "active",
                    },
                })
            db.commit()
            vector_store.add_user_memories(entries)
        except Exception:
            db.rollback()
            logger.exception("用户记忆入库失败（user=%s，session=%s）", user_id, session_id)
            return 0
        finally:
            db.close()
        logger.info("用户长期记忆入库 %d 条（user=%s）", len(entries), user_id)
        return len(entries)

    def recall(self, query: str, user_id: str, top_k: int | None = None) -> list[dict]:
        """仅召回指定用户的记忆；user_id 是不可省略的安全条件。"""
        try:
            query_vector = qwen.embed_query(query)
            rows = vector_store.query_user_memories(
                user_id, query_vector, top_k or settings.memory_recall_top_k
            )
        except Exception:
            logger.exception("记忆召回失败（user=%s）", user_id)
            return []
        return [
            {
                "type": metadata.get("memory_type", "fact"),
                "content": content,
                "score": round(1 - distance, 4),
            }
            for content, metadata, distance in rows
        ]

    def delete_memory_vector(self, memory_id: str) -> None:
        try:
            vector_store.delete_user_memory(memory_id)
        except Exception:
            logger.exception("删除用户记忆向量失败（memory=%s）", memory_id)

    def upsert_memory_vector(self, row: UserMemory) -> None:
        """用户手工编辑记忆后的向量同步。"""
        try:
            embedding = qwen.embed_texts([row.content])[0]
            vector_store.add_user_memories([{
                "id": row.id,
                "content": row.content,
                "embedding": embedding,
                "metadata": {
                    "user_id": row.user_id,
                    "memory_type": row.memory_type,
                    "source_session_id": row.source_session_id or "",
                    "status": "active",
                },
            }])
        except Exception:
            logger.exception("更新用户记忆向量失败（memory=%s）", row.id)

    def clear_user_vectors(self, user_id: str) -> int:
        try:
            return vector_store.delete_user_memories(user_id)
        except Exception:
            logger.exception("清理用户记忆向量失败（user=%s）", user_id)
            return 0


long_term_memory = LongTermMemory()


def format_memories(memories: list[dict]) -> str:
    return "\n".join(
        f"- （{MEMORY_TYPES.get(memory['type'], memory['type'])}）{memory['content']}"
        for memory in memories
    )
