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
QUERY_REWRITE_SYSTEM = """你是一个查询改写器，为知识库检索生成多条检索查询。

规则：
1. 将用户问题改写为 2-3 条适合检索的查询（不含原问题）
2. 改写要面向知识库检索：使用知识库中可能出现的表述方式（术语、别名、产品名全称），
   补充上下文、纠正错别字、拆解多意图
3. 只输出检索查询列表，每行一条，不要编号、不要任何解释

示例：用户问题「千问的定价是多少？」→
通义千问 API 价格
千问模型收费标准
DashScope 计费方式"""  # Phase 3-01 实装
MEMORY_EXTRACT_SYSTEM = """你是一个记忆提取器。分析以下对话，提取值得长期记住的信息。

提取规则：
1. 仅提取非显而易见的、有长期价值的信息（过滤常识、寒暄、一次性闲聊）
2. 记忆类型（type 从以下四类中选择）：
   - user_preference：用户偏好（语言风格、领域偏好、习惯等）
   - key_fact：关键事实（用户提到的重要信息、身份、背景）
   - faq_pair：高频问答对（用户反复问的问题及标准回答）
   - entity：实体信息（人名、产品名、设备、概念关联）
3. confidence 为该信息值得长期记住的置信度（0-1，不重要的给低分）

只输出 JSON 数组，不要输出任何其他内容，格式：
[{"type": "user_preference", "content": "记忆内容", "confidence": 0.9}]"""  # Phase 3-03 实装


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
    memories: str = "",
    system_prompt: str = RAG_ANSWER_SYSTEM,
) -> list[dict]:
    """主场景：System + 参考资料 + 相关记忆 + 对话历史 + 用户问题（§7.4 拼装顺序）。"""
    parts = [f"## 参考资料\n{retrieved}"] if retrieved else []
    if memories:
        parts.append(f"## 相关记忆\n{memories}")
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
    memories: str = "",
    system_prompt: str = NO_CONTEXT_SYSTEM,
) -> list[dict]:
    """检索为空/全被阈值过滤：无参考资料段，明确告知未找到 + 建议（记忆段保留，偏好仍生效）。"""
    parts = []
    if memories:
        parts.append(f"## 相关记忆\n{memories}")
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
