"""千问（DashScope）封装：对话 + Embedding，含重试、超时、维度断言。

API Key 只从 settings 读取；日志中禁止打印 key。
"""
from __future__ import annotations

import itertools
import logging
import time
from collections.abc import Iterator

from dashscope import Generation, TextEmbedding, TextReRank

from app.config import settings
from app.llm.errors import LLMError

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 1024                      # text-embedding-v3 固定维度
EMBEDDING_BATCH_SIZE = 16                 # 每批条数（DashScope 批量上限）
EMBEDDING_BATCH_INTERVAL = 0.2            # 批间间隔，限制 QPS
MAX_RETRIES = 3                           # 429/5xx/网络异常重试次数
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

_KEY_MISSING_HINT = (
    "未配置 DASHSCOPE_API_KEY：请将 .env.example 复制为 .env，"
    "填入阿里云百炼创建的 API Key 后重启服务"
)


def _api_key() -> str:
    key = settings.dashscope_api_key
    if not key:
        raise LLMError(code="api_key_missing", message=_KEY_MISSING_HINT)
    return key


def _retry(fn, *args, **kwargs):
    """指数退避重试：429/5xx 状态码或网络异常时重试，最多 MAX_RETRIES 次。"""
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = fn(*args, **kwargs)
            if resp.status_code == 200:
                return resp
            if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES - 1:
                wait = 2 ** attempt
                logger.warning("DashScope 返回 %s，%.1fs 后重试（第 %d 次）", resp.status_code, wait, attempt + 1)
                time.sleep(wait)
                continue
            raise LLMError(
                code=f"llm_status_{resp.status_code}",
                message=f"模型接口返回错误（{resp.status_code}）：{getattr(resp, 'message', '') or resp.code}",
            )
        except LLMError:
            raise
        except Exception as exc:  # 网络异常等
            last_err = exc
            if attempt < MAX_RETRIES - 1:
                wait = 2 ** attempt
                logger.warning("模型调用异常：%s，%.1fs 后重试（第 %d 次）", exc, wait, attempt + 1)
                time.sleep(wait)
    raise LLMError(code="llm_call_failed", message=f"模型调用失败：{last_err}")


def chat_completion(messages: list[dict], model: str | None = None, stream: bool = False) -> str:
    """非流式对话（流式请用 chat_completion_stream；stream 参数保留仅为兼容旧调用）。"""
    key = _api_key()
    model = model or settings.llm_model
    resp = _retry(
        Generation.call,
        model=model,
        messages=messages,
        api_key=key,
        stream=stream,
        timeout=settings.llm_timeout,
    )
    text = (resp.output or {}).get("text") or ""
    if not text.strip():
        raise LLMError(code="llm_empty_response", message="模型返回为空")
    return text


def _start_stream_with_retry(fn) -> Iterator:
    """流式首块前重试（实测：Generation.call(stream=True) 返回同步生成器）。

    连接级失败（首块 429/5xx）可以整体重试——尚未产出任何 token，无重复风险；
    一旦首块成功（200），后续块不再重试（中途重试会产生重复输出），以
    llm_stream_interrupted 快速失败。返回 chain([first], rest)，首块不丢失。
    """
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            gen = fn()
            first = next(gen)  # 空生成器 → StopIteration 落入 except 走重试
            if first.status_code == 200:
                return itertools.chain([first], gen)
            if first.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES - 1:
                wait = 2 ** attempt
                logger.warning(
                    "流式首块返回 %s，%.1fs 后重试（第 %d 次）", first.status_code, wait, attempt + 1
                )
                time.sleep(wait)
                continue
            raise LLMError(
                code=f"llm_status_{first.status_code}",
                message=f"模型接口返回错误（{first.status_code}）："
                f"{getattr(first, 'message', '') or first.code}",
            )
        except LLMError:
            raise
        except Exception as exc:  # 网络异常、空生成器等
            last_err = exc
            if attempt < MAX_RETRIES - 1:
                wait = 2 ** attempt
                logger.warning("流式调用异常：%s，%.1fs 后重试（第 %d 次）", exc, wait, attempt + 1)
                time.sleep(wait)
    raise LLMError(code="llm_call_failed", message=f"模型调用失败：{last_err}")


def chat_completion_stream(messages: list[dict], model: str | None = None) -> Iterator[str]:
    """流式对话：逐增量 yield 文本（SSE delta 的数据源）。

    实测（2026-08-31 smoke_test_stream.py）：
    - 显式 incremental_output=True 时每块 text 是纯增量（如 '  \\n2'）；
    - 不传时（qwen 系 merge 模式）每块 text 是累积全文。
    因此做差分兜底：text 以 prev 开头 → 按累积处理切出差量，否则按增量处理直接产出，
    两种模式下输出都是正确增量。
    - 流结束块 text 为空且 status=200，跳过；全程无产出 → llm_empty_response（与非流式口径一致）。
    """
    key = _api_key()
    model = model or settings.llm_model
    gen = _start_stream_with_retry(
        lambda: Generation.call(
            model=model,
            messages=messages,
            api_key=key,
            stream=True,
            incremental_output=True,
            timeout=settings.llm_stream_timeout,
        )
    )
    prev = ""
    emitted = False
    for chunk in gen:
        if chunk.status_code != 200:
            raise LLMError(
                code="llm_stream_interrupted",
                message=f"流式输出中断（{chunk.status_code}）：{getattr(chunk, 'message', '') or chunk.code}",
            )
        text = getattr(getattr(chunk, "output", None), "text", None) or ""
        if text.startswith(prev):
            delta = text[len(prev):]  # merge 模式：累积 → 切差量
        else:
            delta = text  # incremental 模式：本身就是增量
        prev = text
        if delta:
            emitted = True
            yield delta
    if not emitted:
        raise LLMError(code="llm_empty_response", message="模型返回为空")


def embed_texts(
    texts: list[str],
    model: str | None = None,
    on_batch: callable = None,
) -> list[list[float]]:
    """批量向量化，16 条/批；断言 1024 维，防静默写脏数据。
    on_batch(done, total) 每批完成后回调，供进度日志/前端使用。"""
    key = _api_key()
    model = model or settings.embedding_model
    results: list[list[float]] = []
    for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = texts[start : start + EMBEDDING_BATCH_SIZE]
        resp = _retry(
            TextEmbedding.call,
            model=model,
            input=batch,
            dimension=EMBEDDING_DIM,
            api_key=key,
            timeout=settings.llm_timeout,
        )
        # 注意：dashscope 1.27 的 TextEmbedding 响应 output 是 dict（实测），
        # 而 Generation 的 output 是对象 —— 两处访问方式不同，勿统一
        embeddings = sorted(resp.output["embeddings"], key=lambda e: e["text_index"])  # 保序
        for item in embeddings:
            vec = item["embedding"]
            if len(vec) != EMBEDDING_DIM:
                raise LLMError(
                    code="embedding_dim_mismatch",
                    message=f"Embedding 维度异常：期望 {EMBEDDING_DIM}，实际 {len(vec)}",
                )
            results.append(vec)
        done = min(start + EMBEDDING_BATCH_SIZE, len(texts))
        if on_batch:
            on_batch(done, len(texts))
        if start + EMBEDDING_BATCH_SIZE < len(texts):
            time.sleep(EMBEDDING_BATCH_INTERVAL)
    if len(results) != len(texts):
        raise LLMError(
            code="embedding_count_mismatch",
            message=f"Embedding 数量异常：期望 {len(texts)}，实际 {len(results)}",
        )
    return results


def embed_query(text: str) -> list[float]:
    """单条查询向量化（复用批量入口，保持同模型同参数）。"""
    return embed_texts([text])[0]


def rerank_texts(
    query: str, documents: list[str], model: str | None = None
) -> list[tuple[int, float]]:
    """DashScope 重排（Phase 3-02）：返回 [(原文下标, relevance_score)] 分数降序。

    SDK 契约（已核对 dashscope 1.27.2 源码）：TextReRank.call(...) →
    ReRankResponse.output.results 为 [{index, relevance_score, document}] 列表，
    index 指向 documents 原列表下标，SDK 已按分数降序返回（此处再排一次保序）。
    """
    key = _api_key()
    resp = _retry(
        TextReRank.call,
        model=model or settings.rerank_model,
        query=query,
        documents=documents,
        top_n=len(documents),
        api_key=key,
        timeout=settings.llm_timeout,
    )
    results = sorted(resp.output.results, key=lambda r: r.relevance_score, reverse=True)
    return [(r.index, r.relevance_score) for r in results]
