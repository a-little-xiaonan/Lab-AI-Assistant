"""按回答依据路由聊天请求；分类结果不充当事实依据。"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from threading import Event
from typing import Literal

from app.config import settings
from app.core import rag_pipeline
from app.core.agent import orchestrator as agent
from app.core.agent.runtime import ExecutionBudget, run_bounded
from app.llm import qwen
from app.llm.errors import LLMError
from app.llm.prompt_templates import build_general_answer_messages, build_intent_route_messages

logger = logging.getLogger(__name__)
PartRoute = Literal["general", "knowledge", "tool"]
Route = Literal["general", "knowledge", "tool", "mixed", "clarify"]
TOOL_UNAVAILABLE = "当前没有可用的实时查询或计算能力，无法核实这部分信息。"
PART_FAILURE = "这部分暂时无法回答，请稍后重试。"


@dataclass(frozen=True)
class IntentPart:
    question: str
    route: PartRoute


@dataclass(frozen=True)
class IntentPlan:
    route: Route
    parts: tuple[IntentPart, ...] = ()
    clarifying_question: str = ""


def _parse_plan(output: str) -> IntentPlan:
    try:
        start, end = output.index("{"), output.rindex("}")
        data = json.loads(output[start:end + 1])
        if not isinstance(data, dict):
            raise ValueError("not an object")
        route = data["route"]
        if route not in {"general", "knowledge", "tool", "mixed", "clarify"}:
            raise ValueError("unknown route")
        raw_parts = data.get("parts", [])
        if not isinstance(raw_parts, list):
            raise ValueError("invalid parts")
        if route == "mixed":
            if not 2 <= len(raw_parts) <= 3:
                raise ValueError("invalid part count")
            parts = []
            for item in raw_parts:
                if not isinstance(item, dict):
                    raise ValueError("invalid part")
                question, kind = item.get("question"), item.get("route")
                if not isinstance(question, str) or not question.strip() or len(question) > 4000:
                    raise ValueError("invalid subquestion")
                if kind not in {"general", "knowledge", "tool"}:
                    raise ValueError("invalid part route")
                parts.append(IntentPart(question.strip(), kind))
            if len({p.question for p in parts}) != len(parts) or len({p.route for p in parts}) < 2:
                raise ValueError("not independent mixed parts")
            return IntentPlan(route="mixed", parts=tuple(parts))
        if raw_parts:
            raise ValueError("unexpected parts")
        if route == "clarify":
            question = data.get("clarifying_question")
            if not isinstance(question, str) or not question.strip() or len(question) > 200:
                raise ValueError("invalid clarification")
            return IntentPlan(route="clarify", clarifying_question=question.strip())
        return IntentPlan(route=route)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise LLMError("intent_route_failed", "问题分类结果无效，请重试。") from exc


def _classify(query: str, history: str, cancellation: Event | None = None) -> IntentPlan:
    started = time.monotonic()
    budget = ExecutionBudget(started + settings.intent_route_timeout_seconds)
    if cancellation is not None:
        budget.cancelled = cancellation
    try:
        output = run_bounded(budget, qwen.chat_completion, build_intent_route_messages(query, history))
    except (LLMError, TimeoutError, RuntimeError) as exc:
        logger.warning("回答依据分类失败：%s", type(exc).__name__)
        raise LLMError("intent_route_failed", "问题分类暂时失败，请稍后重试。") from exc
    plan = _parse_plan(output)
    logger.info("回答依据路由：route=%s parts=%d elapsed_ms=%d",
                plan.route, len(plan.parts), int((time.monotonic() - started) * 1000))
    return plan


def _answer_one(route: PartRoute, query: str, kb_ids: list[str], session_id: str | None,
                user_id: str | None, history: str) -> dict:
    if route == "general":
        text = qwen.chat_completion(build_general_answer_messages(query, history))
        return {"answer": text.strip(), "sources": []}
    if route == "knowledge":
        handler = agent.answer if settings.agent_enabled else rag_pipeline.answer
        return handler(query, kb_ids, session_id=session_id, user_id=user_id)
    if settings.agent_enabled:
        return agent.answer(query, kb_ids, session_id=session_id, user_id=user_id)
    return {"answer": TOOL_UNAVAILABLE, "sources": []}


def _merge_sources(results: list[dict], key: str) -> list[dict]:
    merged, seen = [], set()
    for result in results:
        for source in result.get(key, []):
            identity = json.dumps(source, ensure_ascii=False, sort_keys=True)
            if identity not in seen:
                seen.add(identity)
                merged.append(source)
    return merged


def answer(query: str, kb_ids: list[str], session_id: str | None = None,
           user_id: str | None = None, **flags) -> dict:
    history = rag_pipeline._get_history_context(session_id)
    plan = _classify(query, history)
    if plan.route == "clarify":
        return {"answer": plan.clarifying_question, "sources": []}
    if plan.route != "mixed":
        return _answer_one(plan.route, query, kb_ids, session_id, user_id, history)
    sections, results = [], []
    for index, part in enumerate(plan.parts, 1):
        try:
            result = _answer_one(part.route, part.question, kb_ids, session_id, user_id, history)
        except LLMError:
            logger.exception("混合问题子项回答失败：route=%s", part.route)
            result = {"answer": PART_FAILURE, "sources": []}
        results.append(result)
        sections.append(f"{index}. {part.question}\n{result['answer']}")
    merged = {"answer": "\n\n".join(sections), "sources": _merge_sources(results, "sources")}
    if any("tool_sources" in result for result in results):
        merged["tool_sources"] = _merge_sources(results, "tool_sources")
    return merged


def _direct_stream(text: str) -> Iterator[dict]:
    yield {"type": "delta", "text": text}
    yield {"type": "done", "full_text": text, "sources": []}


def _stream_one(route: PartRoute, query: str, kb_ids: list[str], session_id: str | None,
                user_id: str | None, history: str, cancellation: Event | None) -> Iterator[dict]:
    if route == "general":
        parts = []
        for delta in qwen.chat_completion_stream(build_general_answer_messages(query, history)):
            if cancellation is not None and cancellation.is_set():
                return
            parts.append(delta)
            yield {"type": "delta", "text": delta}
        yield {"type": "done", "full_text": "".join(parts).strip(), "sources": []}
        return
    if route == "knowledge":
        if settings.agent_enabled:
            yield from agent.answer_stream(query, kb_ids, session_id=session_id, user_id=user_id,
                                           cancellation=cancellation)
        else:
            yield from rag_pipeline.answer_stream(query, kb_ids, session_id=session_id, user_id=user_id,
                                                  cancellation=cancellation)
        return
    if settings.agent_enabled:
        yield from agent.answer_stream(query, kb_ids, session_id=session_id, user_id=user_id,
                                       cancellation=cancellation)
    else:
        yield from _direct_stream(TOOL_UNAVAILABLE)


def answer_stream(query: str, kb_ids: list[str], session_id: str | None = None,
                  user_id: str | None = None, **flags) -> Iterator[dict]:
    cancellation = flags.get("cancellation")
    history = rag_pipeline._get_history_context(session_id)
    plan = _classify(query, history, cancellation)
    if cancellation is not None and cancellation.is_set():
        return
    if plan.route == "clarify":
        yield from _direct_stream(plan.clarifying_question)
        return
    if plan.route != "mixed":
        yield from _stream_one(plan.route, query, kb_ids, session_id, user_id, history, cancellation)
        return

    emitted, results = [], []
    for index, part in enumerate(plan.parts, 1):
        if cancellation is not None and cancellation.is_set():
            return
        heading = ("\n\n" if index > 1 else "") + f"{index}. {part.question}\n"
        emitted.append(heading)
        yield {"type": "delta", "text": heading}
        completion = None
        part_deltas = []
        try:
            for item in _stream_one(part.route, part.question, kb_ids, session_id, user_id,
                                    history, cancellation):
                if cancellation is not None and cancellation.is_set():
                    return
                if item["type"] == "delta":
                    part_deltas.append(item["text"])
                elif item["type"] == "status":
                    yield item
                elif item["type"] == "done":
                    completion = item
            if cancellation is not None and cancellation.is_set():
                return
            if completion is None:
                raise LLMError("incomplete_stream", "回答未完整生成")
            part_text = "".join(part_deltas)
            if part_text:
                emitted.append(part_text)
                yield {"type": "delta", "text": part_text}
        except LLMError:
            logger.exception("混合问题子项流式回答失败：route=%s", part.route)
            emitted.append(PART_FAILURE)
            yield {"type": "delta", "text": PART_FAILURE}
            completion = {"sources": []}
        results.append(completion)
    done = {"type": "done", "full_text": "".join(emitted),
            "sources": _merge_sources(results, "sources")}
    if any("tool_sources" in result for result in results):
        done["tool_sources"] = _merge_sources(results, "tool_sources")
    yield done
