"""Prompt 构建观测：只记录模板版本、指纹、角色、长度和受控 ID，不落正文。"""
from __future__ import annotations

import hashlib

from app.observability.tracing import (
    bounded_ids, current_trace, is_truncated, stage_for,
)


def prompt_fingerprint(messages: list[dict]) -> str:
    digest = hashlib.sha256()
    for message in messages:
        digest.update(str(message.get("role", "")).encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(message.get("content", "")).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:32]


def record_prompt_build(
    purpose: str,
    messages: list[dict],
    *,
    template_version: str,
    history_message_ids=(),
    evidence_ids=(),
    budget_tokens: int | None = None,
    trimmed: bool = False,
) -> str:
    """记录一次 Prompt 投影；指纹只用于比对，不能反推正文。"""
    if current_trace() is None:
        return ""
    try:
        fingerprint = prompt_fingerprint(messages)
        roles = [str(message.get("role", ""))[:16] for message in messages][:10]
        chars = sum(len(str(message.get("content", ""))) for message in messages)
        history_ids = bounded_ids(history_message_ids)
        evidence = bounded_ids(evidence_ids)
        with stage_for(
            "model",
            "prompt.build",
            purpose=purpose,
            template_version=template_version,
            prompt_fingerprint=fingerprint,
            message_roles=roles,
            message_count=len(messages),
            input_chars=chars,
            history_message_ids=history_ids,
            evidence_ids=evidence,
            budget_tokens=budget_tokens,
            trimmed=trimmed
            or is_truncated(history_message_ids)
            or is_truncated(evidence_ids),
        ):
            pass
        return fingerprint
    except Exception:
        # 观测失败不能改变 Prompt 或回答路径。
        return ""
