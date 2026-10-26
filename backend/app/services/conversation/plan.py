"""多轮动作计划的数据契约与严格解析器；模型输出只作为候选，不直接执行。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from app.llm.prompt_templates import CONVERSATION_PLAN_VERSION

Action = Literal["answer", "explain_source", "transform", "clarify"]
Basis = Literal["general", "knowledge", "tool", "mixed"]
PartBasis = Literal["general", "knowledge", "tool"]
TransformKind = Literal["summarize", "simplify", "reformat"]
Resolution = Literal["supplement", "new_topic", "cancel", "unclear"]

_ACTIONS = {"answer", "explain_source", "transform", "clarify"}
_BASES = {"general", "knowledge", "tool", "mixed"}
_PART_BASES = {"general", "knowledge", "tool"}
_TRANSFORMS = {"summarize", "simplify", "reformat"}
_RESOLUTIONS = {"supplement", "new_topic", "cancel", "unclear"}
_UNSAFE_DRAFT_KEYS = (
    "api_key", "apikey", "token", "secret", "password", "authorization",
    "cookie", "bearer", "handle", "url", "html", "content", "prompt",
)


class PlanValidationError(ValueError):
    """规划输出不满足服务端契约；调用方必须降级为澄清或错误，不能执行。"""


class TargetValidationError(PlanValidationError):
    """目标缺失、越权或不在候选集合中；应转为澄清而不是系统错误。"""


@dataclass(frozen=True)
class PlanPart:
    question: str
    basis: PartBasis
    reference_message_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ClarificationDraft:
    question: str
    missing_parameters: tuple[str, ...] = ()
    safe_draft: dict | None = None


@dataclass(frozen=True)
class ConversationPlan:
    action: Action
    basis: Basis | None
    reference_message_ids: tuple[int, ...]
    resolved_question: str
    parts: tuple[PlanPart, ...] = ()
    transform_kind: TransformKind | None = None
    clarification: ClarificationDraft | None = None
    clarification_resolution: Resolution | None = None
    planner_version: str = CONVERSATION_PLAN_VERSION


def _text(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _reference_ids(value, candidates: set[int], explicit_target: int | None) -> tuple[int, ...]:
    if value is None:
        raw = []
    elif isinstance(value, list):
        raw = value
    else:
        raise PlanValidationError("reference_message_ids 必须是列表")
    ids = []
    for item in raw[:3]:
        if isinstance(item, bool) or not isinstance(item, int):
            raise TargetValidationError("消息 ID 必须是整数")
        if item not in candidates and item != explicit_target:
            raise TargetValidationError("消息 ID 不在服务端候选集合中")
        if item not in ids:
            ids.append(item)
    return tuple(ids)


def _safe_draft(value) -> dict:
    """最小参数草稿：只保留短标量，丢弃密钥、句柄、正文和链接。"""
    if not isinstance(value, dict):
        return {}
    draft: dict = {}
    for key, item in list(value.items())[:10]:
        name = str(key)[:32]
        if not name or any(word in name.casefold() for word in _UNSAFE_DRAFT_KEYS):
            continue
        if isinstance(item, bool) or isinstance(item, int):
            draft[name] = item
        elif isinstance(item, float) and item == item and abs(item) != float("inf"):
            draft[name] = item
        elif isinstance(item, str):
            text = item.strip()
            lowered = text.casefold()
            if (len(text) > 120 or "http://" in lowered or "https://" in lowered
                    or lowered.startswith(("sk-", "bearer "))):
                continue
            draft[name] = text[:120]
    return draft


def _legacy_route_plan(data: dict, query: str) -> ConversationPlan:
    """把 P2 的 route 输出映射为 P3 计划；不产生 explain_source/transform。"""
    route = data.get("route")
    resolved = _text(data.get("resolved_question"), 4000) or _text(query, 4000)
    if route in {"general", "knowledge", "tool"}:
        return ConversationPlan(
            action="answer", basis=route, reference_message_ids=(),
            resolved_question=resolved,
        )
    if route == "mixed":
        raw_parts = data.get("parts")
        if not isinstance(raw_parts, list) or not 2 <= len(raw_parts) <= 3:
            raise PlanValidationError("mixed 分类结果无效")
        parts = []
        for item in raw_parts:
            if not isinstance(item, dict):
                raise PlanValidationError("子问题格式无效")
            question = _text(item.get("question"), 4000)
            kind = item.get("route", item.get("basis"))
            if not question or kind not in _PART_BASES:
                raise PlanValidationError("子问题字段无效")
            parts.append(PlanPart(question=question, basis=kind))
        if len({part.basis for part in parts}) < 2:
            raise PlanValidationError("mixed 子问题依据类型必须不同")
        return ConversationPlan(
            action="answer", basis="mixed", reference_message_ids=(),
            resolved_question=resolved, parts=tuple(parts),
        )
    if route == "clarify":
        question = _text(data.get("clarifying_question"), 200)
        if not question:
            raise PlanValidationError("澄清问题不能为空")
        return ConversationPlan(
            action="clarify", basis=None, reference_message_ids=(),
            resolved_question=resolved,
            clarification=ClarificationDraft(question=question),
        )
    raise PlanValidationError("未知 route")


def parse_plan(
    output: str,
    *,
    query: str,
    candidate_ids: set[int],
    explicit_target: int | None = None,
    has_pending_clarification: bool = False,
) -> ConversationPlan:
    """解析并校验规划 JSON；失败抛 PlanValidationError。"""
    try:
        start, end = output.index("{"), output.rindex("}")
        data = json.loads(output[start:end + 1])
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise PlanValidationError("规划输出不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise PlanValidationError("规划输出不是对象")

    # 兼容 P2 分类器输出（route 字段）；仅用于平滑过渡和固定桩测试。
    if "action" not in data and "route" in data:
        return _legacy_route_plan(data, query)

    action = data.get("action")
    if action not in _ACTIONS:
        raise PlanValidationError("未知 action")
    basis = data.get("basis")
    if basis is not None and basis not in _BASES:
        raise PlanValidationError("未知 basis")

    resolved_question = _text(data.get("resolved_question"), 4000) or _text(query, 4000)
    if not resolved_question:
        raise PlanValidationError("resolved_question 不能为空")

    parts: list[PlanPart] = []
    if action == "answer" and basis == "mixed":
        raw_parts = data.get("parts")
        if not isinstance(raw_parts, list) or not 2 <= len(raw_parts) <= 3:
            raise PlanValidationError("mixed 计划需要 2～3 个子问题")
        seen = set()
        for item in raw_parts:
            if not isinstance(item, dict):
                raise PlanValidationError("子问题格式无效")
            question = _text(item.get("question"), 4000)
            kind = item.get("route", item.get("basis"))
            if not question or kind not in _PART_BASES:
                raise PlanValidationError("子问题字段无效")
            key = question.casefold()
            if key in seen:
                raise PlanValidationError("子问题重复")
            seen.add(key)
            parts.append(PlanPart(
                question=question,
                basis=kind,
                reference_message_ids=_reference_ids(
                    item.get("reference_message_ids"), candidate_ids, explicit_target
                ) if item.get("reference_message_ids") else (),
            ))
        if len({part.basis for part in parts}) < 2:
            raise PlanValidationError("mixed 子问题依据类型必须不同")
    elif data.get("parts"):
        raise PlanValidationError("非 mixed 计划不应包含子问题")

    transform_kind = data.get("transform_kind")
    if action == "transform":
        if transform_kind not in _TRANSFORMS:
            raise PlanValidationError("改写类型无效")
    else:
        transform_kind = None

    reference_ids = _reference_ids(data.get("reference_message_ids"), candidate_ids, explicit_target)
    if action in {"explain_source", "transform"}:
        if not reference_ids and explicit_target is not None:
            reference_ids = (explicit_target,)
        if not reference_ids:
            raise TargetValidationError("来源追问或改写必须指定目标回答")

    clarification = None
    if action == "clarify":
        raw = data.get("clarification")
        question = _text(raw.get("question"), 200) if isinstance(raw, dict) else ""
        if not question:
            question = _text(data.get("clarifying_question"), 200)
        if not question:
            raise PlanValidationError("澄清问题不能为空")
        missing = []
        if isinstance(raw, dict) and isinstance(raw.get("missing_parameters"), list):
            for item in raw["missing_parameters"][:3]:
                name = _text(item, 64)
                if name and name not in missing:
                    missing.append(name)
        draft = _safe_draft(raw.get("safe_draft")) if isinstance(raw, dict) else {}
        clarification = ClarificationDraft(
            question=question,
            missing_parameters=tuple(missing),
            safe_draft=draft or None,
        )

    resolution = data.get("clarification_resolution")
    if resolution not in _RESOLUTIONS or not has_pending_clarification:
        resolution = None

    if action == "answer" and basis is None:
        raise PlanValidationError("answer 必须指定 basis")
    if action != "answer" and basis is not None:
        raise PlanValidationError("非 answer 动作的 basis 必须为空")
    if action == "answer" and basis == "mixed" and not parts:
        raise PlanValidationError("mixed 计划缺少子问题")

    return ConversationPlan(
        action=action,
        basis=basis,
        reference_message_ids=reference_ids,
        resolved_question=resolved_question,
        parts=tuple(parts),
        transform_kind=transform_kind,
        clarification=clarification,
        clarification_resolution=resolution,
    )
