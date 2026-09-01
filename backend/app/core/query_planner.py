"""复杂问题查询规划：先规则触发，再以严格 JSON 调 LLM 拆为最多三个子问题。

规划失败永远回退单问题，不能成为聊天链路的单点故障。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.config import settings
from app.core.retrieval_topics import retrieval_topics
from app.llm import qwen
from app.llm.prompt_templates import build_query_plan_messages

logger = logging.getLogger(__name__)

_JOIN_WORDS = ("以及", "同时", "分别", "还有", "并且", "和", "、")


@dataclass
class QueryPlan:
    original_query: str
    mode: str
    sub_queries: list[str]
    topic_hints: list[list[str]]

    @property
    def is_multi(self) -> bool:
        return self.mode == "multi" and len(self.sub_queries) > 1


def _single(query: str) -> QueryPlan:
    return QueryPlan(query, "single", [query], [retrieval_topics.hints_for(query)])


def _might_be_complex(query: str) -> bool:
    topics = retrieval_topics.hints_for(query)
    return query.count("？") + query.count("?") >= 2 or len(topics) >= settings.query_plan_trigger_topics or any(
        word in query for word in _JOIN_WORDS
    )


def _parse(query: str, output: str) -> QueryPlan | None:
    """只接受可验证的 JSON；模型夹带解释时截出最外层对象。"""
    try:
        start, end = output.find("{"), output.rfind("}")
        data = json.loads(output[start:end + 1])
    except Exception:
        return None
    if not data.get("needs_decomposition"):
        return _single(query)
    sub_queries: list[str] = []
    hints: list[list[str]] = []
    seen = {query.strip().casefold()}
    for item in data.get("sub_queries", []):
        question = str(item.get("question", "")).strip()[:120]
        key = question.casefold()
        if not question or key in seen:
            continue
        seen.add(key)
        sub_queries.append(question)
        requested = [str(code) for code in item.get("topics", [])]
        hints.append(retrieval_topics.valid_codes(requested) or retrieval_topics.hints_for(question))
        if len(sub_queries) >= settings.query_plan_max_sub_queries:
            break
    if len(sub_queries) < 2:
        return None
    return QueryPlan(query, "multi", sub_queries, hints)


def plan_query(query: str, history_context: str = "") -> QueryPlan:
    """返回单/多问题计划；关闭、规则未命中或任意失败时均返回单问题。"""
    fallback = _single(query)
    if not settings.query_planning_enabled or not _might_be_complex(query):
        return fallback
    try:
        output = qwen.chat_completion(build_query_plan_messages(query, history_context))
        plan = _parse(query, output)
        if plan is None:
            logger.warning("查询规划结果不可信，回退单问题")
            return fallback
        logger.info("查询规划：mode=%s sub_count=%d topics=%s", plan.mode, len(plan.sub_queries), plan.topic_hints)
        return plan
    except Exception:
        logger.exception("查询规划失败，回退单问题")
        return fallback
