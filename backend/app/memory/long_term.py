"""长期记忆（Phase 3-03）：跨会话持久化记忆，存入向量库。

对齐技术设计文档 §7.3 伪代码，一处**关键决策修正**（代码内记录）：
- 伪代码 recall 带 `where={"session_id": ...}`，会把召回限制在当前会话，
  与"跨会话持久化记忆"的本意矛盾（那样长期记忆就退化成短期记忆）
- 本实现：记忆按**知识库（kb）隔离**（collection `kb_{kb_id}_memory`），
  同一 kb 内跨会话共享召回；session_id 仅作为元数据，供清理入口使用

流程：每轮对话结束后台提取（LLM）→ 置信度过滤 → 向量化 upsert 入库；
提问时向量召回相关记忆拼入 prompt（rag_pipeline._prepare 消费）。
提取失败 / JSON 解析失败 / 向量化失败 → 仅记日志，绝不阻塞主回答链路。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

from app.config import settings
from app.llm import qwen
from app.llm.prompt_templates import build_memory_extract_messages
from app.store.vector_store import vector_store

logger = logging.getLogger(__name__)

MEMORY_TYPES = {
    "user_preference": "用户偏好",
    "key_fact": "关键事实",
    "faq_pair": "高频问答对",
    "entity": "实体信息",
}

MEMORY_CONTENT_MAX = 500  # 单条记忆内容上限（字符，防超长脏数据）


def _parse_extraction(text: str) -> list[dict]:
    """容错解析 LLM 输出的 JSON 数组（可能带 ```json 包裹或前后杂文）。"""
    cleaned = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.S)
    if m:
        cleaned = m.group(1).strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("未找到 JSON 数组")
    items = json.loads(cleaned[start : end + 1])
    if not isinstance(items, list):
        raise ValueError("JSON 不是数组")
    out = []
    for item in items:
        if not isinstance(item, dict) or not item.get("content"):
            continue
        out.append(
            {
                "type": item["type"] if item.get("type") in MEMORY_TYPES else "key_fact",
                "content": str(item["content"])[:MEMORY_CONTENT_MAX],
                "confidence": float(item.get("confidence", 0)),
            }
        )
    return out


class LongTermMemory:
    def extract_and_store(
        self, session_id: str, kb_id: str, messages: list[tuple[str, str]]
    ) -> int:
        """提取 + 置信度过滤 + 向量化入库，返回入库条数；失败仅记日志返回 0。"""
        try:
            text = qwen.chat_completion(build_memory_extract_messages(messages))
            items = _parse_extraction(text)
        except Exception:
            logger.exception("记忆提取失败（session=%s），跳过本轮提取", session_id)
            return 0

        kept = [i for i in items if i["confidence"] > settings.memory_confidence_threshold]
        if not kept:
            logger.info("提取 %d 条，置信度 ≤%.1f 过滤后 0 条入库", len(items), settings.memory_confidence_threshold)
            return 0
        try:
            embeddings = qwen.embed_texts([i["content"] for i in kept])
        except Exception:
            logger.exception("记忆向量化失败（session=%s），本轮不入库", session_id)
            return 0

        entries = []
        for item, emb in zip(kept, embeddings):
            # id = session 前缀 + 内容 hash：同 session 同内容 upsert 覆盖（去重）；
            # 清理接口按 session 前缀 + 元数据 session_id 精确删除
            mem_id = (
                f"mem_{session_id[:8]}_"
                f"{hashlib.sha1(item['content'].encode('utf-8')).hexdigest()[:12]}"
            )
            entries.append(
                {
                    "id": mem_id,
                    "content": item["content"],
                    "embedding": emb,
                    "metadata": {
                        "type": item["type"],
                        "session_id": session_id,
                        "kb_id": kb_id,
                    },
                }
            )
        try:
            vector_store.add_memories(kb_id, entries)
        except Exception:
            logger.exception("记忆入库失败（session=%s）", session_id)
            return 0
        logger.info("长期记忆入库 %d 条（session=%s，kb=%s）", len(entries), session_id, kb_id)
        return len(entries)

    def recall(self, query: str, kb_id: str, top_k: int | None = None) -> list[dict]:
        """召回与问题相关的长期记忆（kb 内跨会话共享）。失败返回空列表，不阻断主链路。"""
        try:
            qv = qwen.embed_query(query)
            rows = vector_store.query_memories(kb_id, qv, top_k or settings.memory_recall_top_k)
        except Exception:
            logger.exception("记忆召回失败（kb=%s），本次跳过记忆段", kb_id)
            return []
        return [
            {"type": md.get("type", "key_fact"), "content": text, "score": round(1 - dist, 4)}
            for text, md, dist in rows
        ]

    def clear_session(self, session_id: str, kb_id: str) -> int:
        """清某会话贡献的记忆，返回删除条数。"""
        try:
            return vector_store.delete_session_memories(kb_id, session_id)
        except Exception:
            logger.exception("清理会话记忆失败（session=%s）", session_id)
            return 0


long_term_memory = LongTermMemory()  # 模块级单例


def format_memories(memories: list[dict]) -> str:
    """记忆段格式：- （类型）内容"""
    return "\n".join(
        f"- （{MEMORY_TYPES.get(m['type'], m['type'])}）{m['content']}" for m in memories
    )
