"""查询改写（Phase 3-01）：LLM 扩展查询列表，提升召回率（recall）。

契约：rewrite(query) 永远返回非空列表，首元素恒为原查询。
降级：LLM 失败 / 输出解析失败 / 改写数为 0 → [query]，主链路永不因改写中断。
改写只喂向量侧（hybrid_retriever）；关键词侧默认用原始问题（词面精确优先）；
最终发给 LLM 的 prompt 始终用用户原始问题（rag_pipeline 对改写无感知）。
"""
from __future__ import annotations

import logging
import re

from app.config import settings
from app.llm import qwen
from app.llm.prompt_templates import build_query_rewrite_messages

logger = logging.getLogger(__name__)

REWRITE_LINE_MAX = 200  # 单条改写长度上限


def _parse(text: str) -> list[str]:
    """容错解析：逐行 → 去编号/前缀 → 去空/去重/去与原文相同项 → 截断。"""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        line = re.sub(r"^\d+[\.、)]\s*", "", line)  # "1." "1、" "1)"
        line = line.lstrip("- ").strip()
        if not line:
            continue
        if line in lines:
            continue
        lines.append(line[:REWRITE_LINE_MAX])
    return lines


def rewrite(query: str) -> list[str]:
    """返回 [原查询, *改写]。任何失败 → [原查询]。"""
    if not settings.rewrite_enabled:
        return [query]
    try:
        text = qwen.chat_completion(
            build_query_rewrite_messages(query),
            model=settings.rewrite_model or None,  # 空 → 主模型
        )
        rewritten = _parse(text)
    except Exception:
        logger.exception("查询改写失败，降级为原查询：%s", query[:40])
        return [query]
    if not rewritten:
        return [query]
    logger.info("查询改写：%s → %d 条", query[:30], len(rewritten) + 1)
    return [query, *rewritten[: max(0, settings.rewrite_query_count - 1)]]
