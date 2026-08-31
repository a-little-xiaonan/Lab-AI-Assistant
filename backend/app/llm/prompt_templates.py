"""Prompt 模板（对齐设计文档 §4.2）。

拼装顺序固定：参考资料 → 对话历史 → 用户问题（模型对"最靠近问题的是当前问题"有依赖）。
"""
from __future__ import annotations

from app.core.models import Chunk

SYSTEM_PROMPT = """你是一个专业的知识助手。请仅基于以下参考资料回答用户问题。
如果参考资料中没有相关信息，请明确告知用户你无法找到相关内容。"""


def format_retrieved_chunks(chunks: list[Chunk]) -> str:
    """参考资料段：带 [n] 编号与来源标注，模型可在回答中用 [n] 引用。"""
    lines = []
    for i, c in enumerate(chunks, 1):
        loc = f" P{c.page}" if c.page is not None else ""
        lines.append(f"[{i}] {c.text}\n（来源：{c.source_file}{loc}）")
    return "\n\n".join(lines)


def format_history(history: list[tuple[str, str]]) -> str:
    """对话历史段：MVP 透传（滑动窗口与摘要压缩在 Phase 2-03）。"""
    if not history:
        return ""
    return "\n".join(f"{role}: {content}" for role, content in history)


def build_messages(
    query: str,
    retrieved: str,
    history: str,
    system_prompt: str = SYSTEM_PROMPT,
) -> list[dict]:
    """组装发给模型的 messages：System + 参考资料 + 对话历史 + 用户问题。"""
    parts = [f"## 参考资料\n{retrieved}"] if retrieved else []
    if history:
        parts.append(f"## 对话历史\n{history}")
    parts.append(f"## 用户问题\n{query}")
    user_content = "\n\n".join(parts)
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
