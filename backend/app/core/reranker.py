"""重排器（Phase 3-02）：对 RRF 融合后的候选集精排（precision）。

- RERANKER_TYPE=dashscope（默认）：走 qwen.rerank_texts（DashScope rerank API）
- RERANKER_TYPE=local（可选）：sentence_transformers CrossEncoder（bge-reranker-base），
  transformers 懒导入 + 模型懒加载单例；hf 被墙模型下载高风险，故不默认
- 契约：rerank(query, candidates) 任何异常 → 记日志 + 原顺序返回（重排失败不阻断主链路）
- 重排分数不替代阈值：候选集已在融合层过滤，这里只管排序不再过滤
"""
from __future__ import annotations

import logging

from app.config import settings
from app.core.retriever import RetrievedChunk
from app.llm import qwen

logger = logging.getLogger(__name__)

_local_model = None  # local 后端懒加载单例


def rerank(
    query: str, candidates: list[RetrievedChunk]
) -> list[RetrievedChunk]:
    """重排候选集，返回有序列表。失败降级为原顺序。"""
    if not candidates or len(candidates) < 2:
        return candidates
    try:
        if settings.reranker_type == "local":
            return _rerank_local(query, candidates)
        return _rerank_dashscope(query, candidates)
    except Exception:
        logger.exception("重排失败，降级为 RRF 原顺序（%d 条候选）", len(candidates))
        return candidates


def _rerank_dashscope(
    query: str, candidates: list[RetrievedChunk]
) -> list[RetrievedChunk]:
    ordered = qwen.rerank_texts(query, [c.text for c in candidates])
    by_idx = {i: c for i, c in enumerate(candidates)}
    out: list[RetrievedChunk] = []
    for idx, score in ordered:
        c = by_idx.pop(idx, None)
        if c is None:
            continue  # 响应越界兜底
        c.score = round(score, 4)  # score 语义切换为重排分（观测日志可见）
        out.append(c)
    out.extend(by_idx.values())  # 未返回项兜底（理论上不出现）
    logger.info(
        "重排完成（dashscope）：%d 条 → 前 %d 条（query=%s）",
        len(candidates), len(out), query[:30],
    )
    return out


def _rerank_local(
    query: str, candidates: list[RetrievedChunk]
) -> list[RetrievedChunk]:
    global _local_model
    if _local_model is None:
        from sentence_transformers import CrossEncoder  # 懒导入：未安装 → ImportError 走降级

        _local_model = CrossEncoder("BAAI/bge-reranker-base")
    scores = _local_model.predict([(query, c.text) for c in candidates])
    ranked = sorted(zip(scores, candidates), key=lambda p: p[0], reverse=True)
    for s, c in ranked:
        c.score = round(float(s), 4)
    return [c for _, c in ranked]
