"""Prompt 模板体系（对齐设计文档 §4.2，Phase 2-04 固化）。

场景模板：rag_answer（主）/ no_context（检索为空）/ summarize_history（短期记忆）。
占位模板：query_rewrite / memory_extract（Phase 3 实现，先定义契约）。
拼装顺序固定：参考资料 → 对话历史 → 用户问题（模型对"最靠近问题的是当前问题"有依赖）。
不引入 Jinja2：模板少、变量固定，常量 + f-string 足够。
"""
from __future__ import annotations

from app.core.models import Chunk

RAG_ANSWER_SYSTEM = """你是一个专业的知识助手。请仅基于以下参考资料回答用户问题。
如果参考资料中没有相关信息，请明确告知用户你无法找到相关内容。"""

NO_CONTEXT_SYSTEM = """你是一个专业的知识助手。
知识库中未找到与用户问题相关的内容。请直接以「知识库中未找到与『<问题>』相关的内容」
的句式明确告知用户，并建议更换措辞或上传相关文档。
不要编造信息，不要尝试用常识替代回答。"""

SUMMARIZE_HISTORY_SYSTEM = (
    "你是对话历史压缩器。请将以下对话压缩为一段中文摘要，"
    "保留其中的事实信息与用户偏好（例如用户提到过的身份、产品、需求等）。"
    "只输出摘要正文，不要输出任何其他内容，控制在 300 token 以内。"
)

# ---- Phase 3 占位模板（先定义常量与 builder，实现后置）----
QUERY_REWRITE_SYSTEM = """你是一个查询改写器。请把用户的问题改写得更适合向量检索：
补充上下文、纠正错别字、拆解多意图。只输出改写后的问题。"""  # TODO Phase 3-01
MEMORY_EXTRACT_SYSTEM = """你是一个记忆提取器。请从对话中提取值得长期记住的事实与偏好。
只输出结构化条目，不要输出其他内容。"""  # TODO Phase 3-03


def format_retrieved_chunks(chunks: list[Chunk]) -> str:
    """参考资料段：带 [n] 编号与来源标注，模型可在回答中用 [n] 引用。"""
    lines = []
    for i, c in enumerate(chunks, 1):
        loc = f" P{c.page}" if c.page is not None else ""
        lines.append(f"[{i}] {c.text}\n（来源：{c.source_file}{loc}）")
    return "\n\n".join(lines)


def format_history(history: list[tuple[str, str]]) -> str:
    """对话历史段：role: content 逐行（短期记忆的 get_context 复用同格式）。"""
    if not history:
        return ""
    return "\n".join(f"{role}: {content}" for role, content in history)


def build_rag_answer_messages(
    query: str,
    retrieved: str,
    history: str,
    system_prompt: str = RAG_ANSWER_SYSTEM,
) -> list[dict]:
    """主场景：System + 参考资料 + 对话历史 + 用户问题。"""
    parts = [f"## 参考资料\n{retrieved}"] if retrieved else []
    if history:
        parts.append(f"## 对话历史\n{history}")
    parts.append(f"## 用户问题\n{query}")
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def build_no_context_messages(
    query: str,
    history: str,
    system_prompt: str = NO_CONTEXT_SYSTEM,
) -> list[dict]:
    """检索为空/全被阈值过滤：无参考资料段，明确告知未找到 + 建议。"""
    parts = []
    if history:
        parts.append(f"## 对话历史\n{history}")
    parts.append(f"## 用户问题\n{query}")
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def build_summarize_history_messages(messages: list[tuple[str, str]]) -> list[dict]:
    """短期记忆压缩（Phase 2-03 消费）：只输出摘要正文，≤300 token。"""
    return [
        {"role": "system", "content": SUMMARIZE_HISTORY_SYSTEM},
        {"role": "user", "content": format_history(messages) or "（无对话内容）"},
    ]


def build_query_rewrite_messages(query: str) -> list[dict]:
    """[Phase 3-01 占位] 查询改写。"""
    return [
        {"role": "system", "content": QUERY_REWRITE_SYSTEM},
        {"role": "user", "content": query},
    ]


def build_memory_extract_messages(messages: list[tuple[str, str]]) -> list[dict]:
    """[Phase 3-03 占位] 长期记忆提取。"""
    return [
        {"role": "system", "content": MEMORY_EXTRACT_SYSTEM},
        {"role": "user", "content": format_history(messages) or "（无对话内容）"},
    ]


# 兼容别名：Phase 1 的旧名字（无外部引用后删除）
build_messages = build_rag_answer_messages
SYSTEM_PROMPT = RAG_ANSWER_SYSTEM
