"""短期记忆（Phase 2-03）：会话级滑动窗口 + LLM 摘要压缩。

设计要点（对齐设计文档 §7.2/§6.1）：
- ShortTermMemory 是**运行时视图**：窗口与摘要是进程内内存态，不落库；
  持久层是 messages 表（会话重启后由 load_from_db 重建窗口、重算摘要）
- 压缩触发：消息条数 > max_turns * 2 时，前 max_turns 条交给 LLM 摘要
- 摘要降级：LLM 失败 → 丢弃最旧消息 + 日志，主链路不受影响
- 压缩是额外 LLM 调用（MVP 同步执行；异步化后置，见模块 TODO）
"""
from __future__ import annotations

import logging

from app.config import settings
from app.core.retriever import estimate_tokens
from app.llm import qwen
from app.llm.errors import LLMError
from app.llm.prompt_templates import build_summarize_history_messages

logger = logging.getLogger(__name__)

SUMMARIZE_MAX_TOKENS = 300


def summarize_messages(messages: list[tuple[str, str]]) -> str | None:
    """LLM 摘要一段对话。失败返回 None（调用方降级：丢最旧 + 日志），不抛异常。"""
    try:
        text = qwen.chat_completion(build_summarize_history_messages(messages))
        if estimate_tokens(text) > SUMMARIZE_MAX_TOKENS:
            logger.warning("摘要超过 %d token，截断处理", SUMMARIZE_MAX_TOKENS)
            text = text[: SUMMARIZE_MAX_TOKENS * 2]  # 中文粗截断（1字≈1token 的宽松上限）
        return text.strip() or None
    except LLMError as exc:
        logger.warning("对话摘要失败（%s），调用方降级", exc.code)
        return None
    except Exception:
        logger.exception("对话摘要未知异常")
        return None


class ShortTermMemory:
    """单会话记忆窗口：messages（role, content）对 + 摘要。"""

    def __init__(self, session_id: str, max_turns: int | None = None) -> None:
        self.session_id = session_id
        self.max_turns = max_turns or settings.history_max_turns
        self.messages: list[tuple[str, str]] = []
        self.summary: str | None = None

    # ----- 写入 -----

    def add_message(self, role: str, content: str) -> None:
        """追加消息；超过 max_turns*2 条时压缩旧消息为摘要。"""
        self.messages.append((role, content))
        if len(self.messages) > self.max_turns * 2:
            self._compress()

    def _compress(self) -> None:
        """前 max_turns 条 → 摘要；摘要失败则丢弃最旧（降级不中断主链路）。"""
        old = self.messages[: self.max_turns]
        summary = summarize_messages(old)
        if summary:
            self.summary = f"{self.summary}\n{summary}" if self.summary else summary
        else:
            logger.warning("压缩失败，丢弃 %d 条最旧消息（session=%s）", len(old), self.session_id)
        self.messages = self.messages[self.max_turns:]

    def _rebuild(self, pairs: list[tuple[str, str]]) -> None:
        """从 DB 重建（load_from_db 用）：一次性压缩头部，避免逐条 add 触发多次压缩。"""
        if len(pairs) > self.max_turns * 2:
            keep = self.max_turns * 2
            old = pairs[:-keep]
            summary = summarize_messages(old)
            if summary:
                self.summary = summary
            else:
                logger.warning(
                    "重建窗口摘要失败，丢弃 %d 条旧消息（session=%s）", len(old), self.session_id
                )
            pairs = pairs[-keep:]
        self.messages = list(pairs)

    # ----- 读取 -----

    def get_context(self) -> str:
        """历史上下文（对齐 §7.2 拼装格式）：[历史摘要] + 窗口内消息。"""
        parts = []
        if self.summary:
            parts.append(f"[历史摘要] {self.summary}")
        parts.extend(f"{role}: {content}" for role, content in self.messages)
        return "\n\n".join(parts)

    def trim_to_token_budget(self, max_tokens: int) -> int:
        """超 token 预算时丢弃最旧窗口消息（保留至少 1 条），返回丢弃条数。"""
        dropped = 0
        while (
            self.messages
            and len(self.messages) > 1
            and estimate_tokens(self.get_context()) > max_tokens
        ):
            self.messages.pop(0)
            dropped += 1
        if dropped:
            logger.warning("历史超预算，丢弃 %d 条最旧消息（session=%s）", dropped, self.session_id)
        return dropped
