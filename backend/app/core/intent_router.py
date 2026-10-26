"""按回答依据路由聊天请求；P3 起先做多轮动作规划，分类结果不充当事实依据。"""
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
from app.llm.prompt_templates import (
    CONVERSATION_PLAN_VERSION,
    TRANSFORM_PROMPT_VERSION,
    build_conversation_plan_messages,
    build_general_answer_messages,
    build_intent_route_messages,
    build_transform_messages,
)
from app.models.database import ChatSession
from app.observability.prompt import record_prompt_build
from app.observability.tracing import stage, traced_stream
from app.services.conversation.context import ConversationContext
from app.services.conversation.plan import (
    ClarificationDraft,
    ConversationPlan,
    PlanValidationError,
    TargetValidationError,
    parse_plan,
)
from app.services.conversation.provenance import SourceExplanation, explain_source, load_provenance
from app.services.conversation.reference import (
    TargetResolutionError,
    resolve_explicit_target,
    resolve_targets,
    target_context,
)

logger = logging.getLogger(__name__)
PartRoute = Literal["general", "knowledge", "tool"]
Route = Literal["general", "knowledge", "tool", "mixed", "clarify"]
TOOL_UNAVAILABLE = "当前没有可用的实时查询或计算能力，无法核实这部分信息。"
PART_FAILURE = "这部分暂时无法回答，请稍后重试。"
CLARIFY_TARGET = "请说明你指的是哪一条回答？"


@dataclass(frozen=True)
class IntentPart:
    question: str
    route: PartRoute
    reference_message_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class IntentPlan:
    route: Route
    parts: tuple[IntentPart, ...] = ()
    clarifying_question: str = ""


@dataclass(frozen=True)
class PlannedTurn:
    """已验证的 P3 计划与纯数据上下文，不跨线程持有数据库对象。"""

    plan: ConversationPlan
    target_message_ids: tuple[int, ...] = ()
    reference_context: str = ""
    target_text: str = ""
    explanation: SourceExplanation | None = None
    target_basis_status: str = "unknown"


def _provenance_for(route: PartRoute) -> dict:
    answer_kind = "unavailable" if route == "tool" and not settings.agent_enabled else route
    return {
        "action": "answer",
        "answer_kind": answer_kind,
        "basis_status": (
            "not_applicable" if route == "general"
            else "unknown" if answer_kind == "unavailable"
            else "recorded"
        ),
        "parts": [],
    }


def _parse_plan(output: str) -> IntentPlan:
    """P2 分类器解析；保留给关闭多轮规划时的回退路径。"""
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
    with stage("intent.classify", query_chars=len(query), history_chars=len(history[-2000:])) as observed:
        plan = _classify_impl(query, history, cancellation)
        observed.set_attributes(route=plan.route, parts_count=len(plan.parts))
        return plan


def _classify_impl(query: str, history: str, cancellation: Event | None = None) -> IntentPlan:
    started = time.monotonic()
    budget = ExecutionBudget(started + settings.intent_route_timeout_seconds)
    if cancellation is not None:
        budget.cancelled = cancellation
    try:
        output = run_bounded(
            budget,
            lambda: qwen.chat_completion(
                build_intent_route_messages(query, history),
                purpose="conversation_plan",
            ),
        )
    except (LLMError, TimeoutError, RuntimeError) as exc:
        logger.warning("回答依据分类失败：%s", type(exc).__name__)
        raise LLMError("intent_route_failed", "问题分类暂时失败，请稍后重试。") from exc
    plan = _parse_plan(output)
    logger.info("回答依据路由：route=%s parts=%d elapsed_ms=%d",
                plan.route, len(plan.parts), int((time.monotonic() - started) * 1000))
    return plan


def _answer_one(route: PartRoute, query: str, kb_ids: list[str], session_id: str | None,
                user_id: str | None, history: str, audience_scope: str,
                cancellation: Event | None = None, include_provenance: bool = False,
                history_message_ids=(), plan_context=None, part_id: str | None = None) -> dict:
    with stage("answer.execute", route=route, part_id=part_id) as observed:
        result = _answer_one_impl(
            route, query, kb_ids, session_id, user_id, history, audience_scope,
            cancellation, include_provenance, history_message_ids, plan_context,
        )
        observed.set_attributes(source_count=len(result.get("sources", [])),
                                tool_source_count=len(result.get("tool_sources", [])))
        return result


def _answer_one_impl(route: PartRoute, query: str, kb_ids: list[str], session_id: str | None,
                     user_id: str | None, history: str, audience_scope: str,
                     cancellation: Event | None = None,
                     include_provenance: bool = False,
                     history_message_ids=(), plan_context=None) -> dict:
    if route == "general":
        messages = build_general_answer_messages(query, history)
        record_prompt_build("answer_general", messages, template_version="general-answer-v1",
                            history_message_ids=history_message_ids)
        text = qwen.chat_completion(messages, purpose="answer")
        with stage("output.normalize", input_chars=len(text),
                   processor_version="general-trim-v1") as observed:
            answer = text.strip()
            observed.set_attributes(output_chars=len(answer),
                                    normalized=answer != text, answer_mode="general")
        return {"answer": answer, "sources": []}
    if route == "knowledge":
        handler = agent.answer if settings.agent_enabled else rag_pipeline.answer
        return handler(query, kb_ids, session_id=session_id, user_id=user_id,
                       audience_scope=audience_scope, history_context=history,
                       cancellation=cancellation, include_provenance=include_provenance,
                       history_message_ids=history_message_ids,
                       conversation_plan=plan_context)
    if settings.agent_enabled:
        return agent.answer(query, kb_ids, session_id=session_id, user_id=user_id,
                            history_context=history, cancellation=cancellation,
                            include_provenance=include_provenance,
                            history_message_ids=history_message_ids,
                            conversation_plan=plan_context)
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


def _execute_intent_plan(plan: IntentPlan, query: str, kb_ids: list[str],
                         session_id: str | None, user_id: str | None, history: str,
                         audience_scope: str, cancellation: Event | None = None,
                         include_provenance: bool = False,
                         emit_selected: bool = True,
                         history_message_ids=(), plan_context=None) -> dict:
    """P2 路由计划的统一非流式执行器，P3 answer 动作复用。"""
    if emit_selected:
        with stage("intent.selected", route=plan.route, parts_count=len(plan.parts)):
            pass
    if plan.route == "clarify":
        result = {"answer": plan.clarifying_question, "sources": []}
        if include_provenance:
            result["_provenance"] = {
                "action": "clarify", "answer_kind": "clarification",
                "basis_status": "not_applicable", "parts": [],
            }
        return result
    if plan.route != "mixed":
        result = _answer_one(
            plan.route, query, kb_ids, session_id, user_id, history, audience_scope,
            cancellation, include_provenance, history_message_ids, plan_context,
        )
        if include_provenance:
            result["_provenance"] = _provenance_for(plan.route)
        return result
    sections, results, part_records = [], [], []
    for index, part in enumerate(plan.parts, 1):
        try:
            result = _answer_one(
                part.route, part.question, kb_ids, session_id, user_id, history,
                audience_scope, cancellation, include_provenance,
                history_message_ids, plan_context, part_id=f"part-{index}",
            )
            part_status = "completed"
        except LLMError:
            logger.exception("混合问题子项回答失败：route=%s", part.route)
            result = {"answer": PART_FAILURE, "sources": []}
            part_status = "failed"
        results.append(result)
        part_records.append({"answer_kind": _provenance_for(part.route)["answer_kind"],
                             "status": part_status,
                             "reference_message_ids": list(part.reference_message_ids)})
        sections.append(f"{index}. {part.question}\n{result['answer']}")
    merged = {"answer": "\n\n".join(sections), "sources": _merge_sources(results, "sources")}
    if any("tool_sources" in result for result in results):
        merged["tool_sources"] = _merge_sources(results, "tool_sources")
    if include_provenance:
        merged["_evidence_refs"] = _merge_sources(results, "_evidence_refs")
        merged["_provenance"] = {
            "action": "answer", "answer_kind": "mixed", "basis_status": "recorded",
            "parts": part_records,
        }
    return merged


def _execute_intent_plan_stream(plan: IntentPlan, query: str, kb_ids: list[str],
                                session_id: str | None, user_id: str | None,
                                history: str, audience_scope: str,
                                cancellation: Event | None = None,
                                include_provenance: bool = False,
                                emit_selected: bool = True,
                                history_message_ids=(), plan_context=None) -> Iterator[dict]:
    if emit_selected:
        with stage("intent.selected", route=plan.route, parts_count=len(plan.parts)):
            pass
    if plan.route == "clarify":
        for item in _direct_stream(plan.clarifying_question):
            if item["type"] == "done" and include_provenance:
                item["_provenance"] = {
                    "action": "clarify", "answer_kind": "clarification",
                    "basis_status": "not_applicable", "parts": [],
                }
            yield item
        return
    if plan.route != "mixed":
        for item in _stream_one(plan.route, query, kb_ids, session_id, user_id, history,
                                cancellation, audience_scope, include_provenance,
                                history_message_ids, plan_context):
            if item["type"] == "done" and include_provenance:
                item["_provenance"] = _provenance_for(plan.route)
            yield item
        return

    emitted, results, part_records = [], [], []
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
                                    history, cancellation, audience_scope,
                                    include_provenance, history_message_ids, plan_context,
                                    part_id=f"part-{index}"):
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
            part_status = "completed"
        except LLMError:
            logger.exception("混合问题子项流式回答失败：route=%s", part.route)
            emitted.append(PART_FAILURE)
            yield {"type": "delta", "text": PART_FAILURE}
            completion = {"sources": []}
            part_status = "failed"
        results.append(completion)
        part_records.append({"answer_kind": _provenance_for(part.route)["answer_kind"],
                             "status": part_status,
                             "reference_message_ids": list(part.reference_message_ids)})
    done = {"type": "done", "full_text": "".join(emitted),
            "sources": _merge_sources(results, "sources")}
    if any("tool_sources" in result for result in results):
        done["tool_sources"] = _merge_sources(results, "tool_sources")
    if include_provenance:
        done["_evidence_refs"] = _merge_sources(results, "_evidence_refs")
        done["_provenance"] = {
            "action": "answer", "answer_kind": "mixed", "basis_status": "recorded",
            "parts": part_records,
        }
    yield done


def _direct_stream(text: str) -> Iterator[dict]:
    yield {"type": "delta", "text": text}
    yield {"type": "done", "full_text": text, "sources": []}


def _stream_one(route: PartRoute, query: str, kb_ids: list[str], session_id: str | None,
                user_id: str | None, history: str, cancellation: Event | None,
                audience_scope: str, include_provenance: bool = False,
                history_message_ids=(), plan_context=None,
                part_id: str | None = None) -> Iterator[dict]:
    yield from traced_stream(
        _stream_one_impl(route, query, kb_ids, session_id, user_id, history, cancellation,
                         audience_scope, include_provenance, history_message_ids, plan_context),
        route=route,
        part_id=part_id,
    )


def _stream_one_impl(route: PartRoute, query: str, kb_ids: list[str], session_id: str | None,
                     user_id: str | None, history: str, cancellation: Event | None,
                     audience_scope: str, include_provenance: bool = False,
                     history_message_ids=(), plan_context=None) -> Iterator[dict]:
    if route == "general":
        messages = build_general_answer_messages(query, history)
        record_prompt_build("answer_general", messages, template_version="general-answer-v1",
                            history_message_ids=history_message_ids)
        parts = []
        for delta in qwen.chat_completion_stream(messages, purpose="answer"):
            if cancellation is not None and cancellation.is_set():
                return
            parts.append(delta)
            yield {"type": "delta", "text": delta}
        raw = "".join(parts)
        with stage("output.normalize", input_chars=len(raw),
                   processor_version="general-trim-v1") as observed:
            answer = raw.strip()
            observed.set_attributes(output_chars=len(answer),
                                    normalized=answer != raw, answer_mode="general")
        yield {"type": "done", "full_text": answer, "sources": []}
        return
    if route == "knowledge":
        if settings.agent_enabled:
            yield from agent.answer_stream(query, kb_ids, session_id=session_id, user_id=user_id,
                                           cancellation=cancellation, history_context=history,
                                           include_provenance=include_provenance,
                                           history_message_ids=history_message_ids,
                                           conversation_plan=plan_context)
        else:
            yield from rag_pipeline.answer_stream(query, kb_ids, session_id=session_id, user_id=user_id,
                                                  cancellation=cancellation, audience_scope=audience_scope,
                                                  history_context=history,
                                                  include_provenance=include_provenance,
                                                  history_message_ids=history_message_ids)
        return
    if settings.agent_enabled:
        yield from agent.answer_stream(query, kb_ids, session_id=session_id, user_id=user_id,
                                       cancellation=cancellation, history_context=history,
                                       include_provenance=include_provenance,
                                       history_message_ids=history_message_ids,
                                       conversation_plan=plan_context)
    else:
        yield from _direct_stream(TOOL_UNAVAILABLE)


# ----- P3：多轮动作规划与执行 -----

def _fallback_plan(query: str, reason: str) -> ConversationPlan:
    question = CLARIFY_TARGET if ("目标" in reason or "回答" in reason) else "请再具体说明一下你的问题。"
    return ConversationPlan(
        action="clarify",
        basis=None,
        reference_message_ids=(),
        resolved_question=query[:4000],
        clarification=ClarificationDraft(question=question),
    )


def _plan_turn(
    query: str,
    context: ConversationContext,
    cancellation: Event | None = None,
) -> PlannedTurn:
    """调用规划模型并在服务端完成字段、目标与依据校验。"""
    if cancellation is not None and cancellation.is_set():
        raise LLMError("cancelled", "回答已取消")
    with stage("conversation.plan", plan_version=CONVERSATION_PLAN_VERSION) as observed:
        db = context.open_db()
        try:
            session = db.get(ChatSession, context.session_id)
            if session is None or session.deleted_at is not None:
                raise LLMError("session_state_changed", "会话状态已变化，请刷新后重试")
            try:
                explicit_target = resolve_explicit_target(
                    db, session, context.target_message_id
                )
            except TargetResolutionError as exc:
                raise LLMError("invalid_target_message", "引用的回答不属于当前会话") from exc
            candidates = {ref.message_id for ref in context.snapshot.recent_answer_refs}
            pending = context.snapshot.pending_clarification
            messages = build_conversation_plan_messages(
                query,
                context.snapshot.planner_text,
                context.snapshot.candidate_text(),
                json.dumps(pending, ensure_ascii=False) if pending else "",
            )
            record_prompt_build(
                "conversation_plan",
                messages,
                template_version=CONVERSATION_PLAN_VERSION,
                history_message_ids=context.snapshot.message_ids,
            )
            budget = ExecutionBudget(
                time.monotonic() + settings.conversation_plan_timeout_seconds
            )
            if cancellation is not None:
                budget.cancelled = cancellation
            started = time.monotonic()
            try:
                output = run_bounded(
                    budget,
                    lambda: qwen.chat_completion(messages, purpose="conversation_plan"),
                )
            except (LLMError, TimeoutError, RuntimeError) as exc:
                logger.warning("多轮规划调用失败：%s", type(exc).__name__)
                raise LLMError(
                    "conversation_plan_failed", "问题规划暂时失败，请稍后重试。"
                ) from exc
            logger.info("多轮规划：elapsed_ms=%d", int((time.monotonic() - started) * 1000))
            try:
                plan = parse_plan(
                    output,
                    query=query,
                    candidate_ids=candidates,
                    explicit_target=explicit_target.id if explicit_target else None,
                    has_pending_clarification=bool(pending),
                )
            except TargetValidationError as exc:
                plan = _fallback_plan(query, str(exc))
                observed.set_attributes(clarification_reason=str(exc)[:80])
            except PlanValidationError as exc:
                raise LLMError(
                    "conversation_plan_failed", "问题规划结果无效，请稍后重试。"
                ) from exc

            targets = ()
            if (plan.action in {"explain_source", "transform"}
                    or plan.reference_message_ids or explicit_target is not None):
                try:
                    targets = resolve_targets(
                        db, session, plan,
                        explicit_target=explicit_target,
                        candidate_ids=candidates,
                        pending_clarification=pending,
                    )
                except TargetResolutionError as exc:
                    plan = _fallback_plan(query, str(exc))
                    observed.set_attributes(clarification_reason=str(exc)[:80])
                    targets = ()
            if plan.action in {"explain_source", "transform"} and not targets:
                plan = _fallback_plan(query, "缺少可验证的目标回答")
                observed.set_attributes(clarification_reason="missing_verified_target")

            reference_context = target_text = ""
            explanation = None
            target_basis_status = "unknown"
            if targets:
                if plan.action == "transform":
                    target_text = targets[0].content[:settings.transform_target_max_chars]
                    provenance = load_provenance(db, targets[0].id)
                    target_basis_status = provenance.basis_status if provenance else "unknown"
                elif plan.action == "answer":
                    reference_context = target_context(db, session, targets)
                elif plan.action == "explain_source":
                    explanation = explain_source(
                        db, targets[0], list(context.readable_kb_ids)
                    )
            observed.set_attributes(
                action=plan.action,
                basis=plan.basis,
                reference_message_ids=list(plan.reference_message_ids),
                target_count=len(targets),
                resolved_question_chars=len(plan.resolved_question),
            )
            return PlannedTurn(
                plan=plan,
                target_message_ids=tuple(target.id for target in targets),
                reference_context=reference_context,
                target_text=target_text,
                explanation=explanation,
                target_basis_status=target_basis_status,
            )
        finally:
            db.close()


def _clarification_payload(plan: ConversationPlan, pending: dict | None,
                           reference_ids: tuple[int, ...] = ()) -> dict | None:
    if (plan.action != "clarify" or plan.clarification is None
            or plan.clarification_resolution == "cancel"):
        return None
    remaining = settings.clarification_max_turns - 1
    if pending and isinstance(pending.get("remaining_turns"), int):
        remaining = pending["remaining_turns"] - 1
    return {
        "question": plan.clarification.question,
        "missing_parameters": list(plan.clarification.missing_parameters),
        "safe_draft": plan.clarification.safe_draft or {},
        "reference_message_ids": list(reference_ids or plan.reference_message_ids),
        "remaining_turns": max(0, remaining),
    }


def _plan_provenance(planned: PlannedTurn, result: dict) -> dict:
    plan = planned.plan
    targets = list(planned.target_message_ids)
    derived: list[int] = []
    if plan.action == "explain_source":
        explanation = planned.explanation
        return {
            "action": "explain_source",
            "answer_kind": "provenance",
            "basis_status": explanation.basis_status if explanation else "unknown",
            "parts": [],
            "reference_message_ids": targets,
            "derived_from_message_ids": [],
            "planner_version": plan.planner_version,
        }
    if plan.action == "transform":
        derived = targets[:1]
        return {
            "action": "transform",
            "answer_kind": "transformed",
            "basis_status": planned.target_basis_status,
            "parts": [],
            "reference_message_ids": targets,
            "derived_from_message_ids": derived,
            "planner_version": plan.planner_version,
        }
    if plan.action == "clarify" or plan.clarification_resolution == "cancel":
        action = "answer" if plan.clarification_resolution == "cancel" else plan.action
        return {
            "action": action,
            "answer_kind": "clarification",
            "basis_status": "not_applicable",
            "parts": [],
            "reference_message_ids": targets or list(plan.reference_message_ids),
            "derived_from_message_ids": [],
            "planner_version": plan.planner_version,
        }
    base = dict(result.get("_provenance") or {})
    base.update({
        "action": "answer",
        "reference_message_ids": targets,
        "derived_from_message_ids": [],
        "planner_version": plan.planner_version,
    })
    return base


def _attach_plan_metadata(result: dict, planned: PlannedTurn,
                          context: ConversationContext) -> None:
    result["_provenance"] = _plan_provenance(planned, result)
    result["_clarification"] = _clarification_payload(
        planned.plan, context.snapshot.pending_clarification,
        planned.target_message_ids,
    )
    result["_plan_action"] = planned.plan.action
    result["_plan_version"] = planned.plan.planner_version
    result["_reference_message_ids"] = list(planned.target_message_ids)
    result["_derived_from_message_ids"] = list(
        result["_provenance"].get("derived_from_message_ids", [])
    )


def _with_reference(history: str, reference_context: str) -> str:
    if not reference_context:
        return history
    block = f"[已确认引用的历史回答]\n{reference_context}"
    return f"{history}\n\n{block}" if history else block


def _plan_context(planned: PlannedTurn) -> dict:
    plan = planned.plan
    return {
        "action": plan.action,
        "basis": plan.basis,
        "reference_message_ids": list(planned.target_message_ids),
        "resolved_question": plan.resolved_question[:1000],
        "parts": [
            {"question": part.question, "basis": part.basis}
            for part in plan.parts
        ],
    }


def _transform_messages(planned: PlannedTurn) -> list[dict]:
    messages = build_transform_messages(
        planned.plan.transform_kind or "simplify",
        planned.target_text,
        planned.plan.resolved_question,
    )
    record_prompt_build(
        "transform",
        messages,
        template_version=TRANSFORM_PROMPT_VERSION,
    )
    return messages


def _answer_with_plan(query: str, kb_ids: list[str], session_id: str | None,
                      user_id: str | None, audience_scope: str,
                      context: ConversationContext, cancellation: Event | None,
                      include_provenance: bool) -> dict:
    planned = _plan_turn(query, context, cancellation)
    plan = planned.plan
    with stage("intent.selected", route=plan.basis or plan.action,
               action=plan.action, basis=plan.basis,
               plan_version=plan.planner_version):
        pass
    if plan.action == "explain_source":
        explanation = planned.explanation
        result = {
            "answer": explanation.text if explanation else "无法确认当时来源。",
            "sources": [],
        }
    elif plan.action == "transform":
        messages = _transform_messages(planned)
        raw = qwen.chat_completion(messages, purpose="answer")
        with stage("output.normalize", input_chars=len(raw),
                   processor_version="citation-strip-v1") as observed:
            answer = rag_pipeline._strip_citations(raw)
            observed.set_attributes(output_chars=len(answer),
                                    normalized=answer != raw, answer_mode="transformed")
        result = {"answer": answer, "sources": []}
    elif plan.clarification_resolution == "cancel":
        result = {"answer": "好的，已取消上一个待补充的问题。", "sources": []}
    elif plan.action == "clarify":
        result = {"answer": plan.clarification.question, "sources": []}
    else:
        intent = IntentPlan(
            route=plan.basis or "general",
            parts=tuple(
                IntentPart(part.question, part.basis, part.reference_message_ids)
                for part in plan.parts
            ),
        )
        history = context.snapshot.text
        if planned.reference_context:
            history = _with_reference(history, planned.reference_context)
        result = _execute_intent_plan(
            intent, plan.resolved_question, kb_ids, session_id, user_id, history,
            audience_scope, cancellation, include_provenance=include_provenance,
            emit_selected=False,
            history_message_ids=context.snapshot.message_ids,
            plan_context=_plan_context(planned),
        )
    if include_provenance:
        _attach_plan_metadata(result, planned, context)
    return result


def _transform_stream(planned: PlannedTurn, cancellation: Event | None) -> Iterator[dict]:
    messages = _transform_messages(planned)
    proc = rag_pipeline.CitationStreamProcessor([])
    parts = []
    for delta in qwen.chat_completion_stream(messages, purpose="answer"):
        if cancellation is not None and cancellation.is_set():
            return
        text = proc.feed(delta)
        if text:
            parts.append(text)
            yield {"type": "delta", "text": text}
    tail = proc.flush()
    if tail:
        parts.append(tail)
        yield {"type": "delta", "text": tail}
    raw = "".join(parts)
    with stage("output.normalize", input_chars=len(raw),
               processor_version="citation-strip-v1") as observed:
        answer = rag_pipeline._strip_citations(raw)
        observed.set_attributes(output_chars=len(answer),
                                normalized=answer != raw, answer_mode="transformed")
    yield {"type": "done", "full_text": answer, "sources": []}


def _answer_with_plan_stream(query: str, kb_ids: list[str], session_id: str | None,
                             user_id: str | None, audience_scope: str,
                             context: ConversationContext, cancellation: Event | None,
                             include_provenance: bool) -> Iterator[dict]:
    if cancellation is not None and cancellation.is_set():
        return
    planned = _plan_turn(query, context, cancellation)
    plan = planned.plan
    with stage("intent.selected", route=plan.basis or plan.action,
               action=plan.action, basis=plan.basis,
               plan_version=plan.planner_version):
        pass
    if cancellation is not None and cancellation.is_set():
        return
    if plan.action == "explain_source":
        text = planned.explanation.text if planned.explanation else "无法确认当时来源。"
        for item in _direct_stream(text):
            if item["type"] == "done" and include_provenance:
                _attach_plan_metadata(item, planned, context)
            yield item
        return
    if plan.clarification_resolution == "cancel":
        for item in _direct_stream("好的，已取消上一个待补充的问题。"):
            if item["type"] == "done" and include_provenance:
                _attach_plan_metadata(item, planned, context)
            yield item
        return
    if plan.action == "clarify":
        for item in _direct_stream(plan.clarification.question):
            if item["type"] == "done" and include_provenance:
                _attach_plan_metadata(item, planned, context)
            yield item
        return
    if plan.action == "transform":
        for item in _transform_stream(planned, cancellation):
            if item["type"] == "done" and include_provenance:
                _attach_plan_metadata(item, planned, context)
            yield item
        return
    intent = IntentPlan(
        route=plan.basis or "general",
        parts=tuple(IntentPart(part.question, part.basis) for part in plan.parts),
    )
    history = context.snapshot.text
    if planned.reference_context:
        history = _with_reference(history, planned.reference_context)
    for item in _execute_intent_plan_stream(
        intent, plan.resolved_question, kb_ids, session_id, user_id, history,
        audience_scope, cancellation, include_provenance=include_provenance,
        emit_selected=False,
        history_message_ids=context.snapshot.message_ids,
        plan_context=_plan_context(planned),
    ):
        if item["type"] == "done" and include_provenance:
            _attach_plan_metadata(item, planned, context)
        yield item


def answer(query: str, kb_ids: list[str], session_id: str | None = None,
           user_id: str | None = None, **flags) -> dict:
    audience_scope = flags.get("audience_scope", "guest")
    context = flags.get("conversation_context")
    if settings.conversation_plan_enabled and context is not None:
        return _answer_with_plan(
            query, kb_ids, session_id, user_id, audience_scope, context,
            flags.get("cancellation"), flags.get("include_provenance", False),
        )
    history = flags.get("history_context")
    if history is None:
        history = rag_pipeline._get_history_context(session_id)
    plan = _classify(query, history, flags.get("cancellation"))
    return _execute_intent_plan(
        plan, query, kb_ids, session_id, user_id, history, audience_scope,
        flags.get("cancellation"), flags.get("include_provenance", False),
    )


def answer_stream(query: str, kb_ids: list[str], session_id: str | None = None,
                  user_id: str | None = None, **flags) -> Iterator[dict]:
    cancellation = flags.get("cancellation")
    audience_scope = flags.get("audience_scope", "guest")
    context = flags.get("conversation_context")
    if settings.conversation_plan_enabled and context is not None:
        yield from _answer_with_plan_stream(
            query, kb_ids, session_id, user_id, audience_scope, context,
            cancellation, flags.get("include_provenance", False),
        )
        return
    history = flags.get("history_context")
    if history is None:
        history = rag_pipeline._get_history_context(session_id)
    plan = _classify(query, history, cancellation)
    yield from _execute_intent_plan_stream(
        plan, query, kb_ids, session_id, user_id, history, audience_scope,
        cancellation, flags.get("include_provenance", False),
    )
