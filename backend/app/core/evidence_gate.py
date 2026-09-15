"""回答证据门槛：在召回候选与生成 Prompt 之间判断资料是否足以回答。

RRF 和余弦相似度只能说明“主题接近”，不能证明片段包含答案。本模块采用三级判定：
1. DashScope 重排分高于接受线时直接放行；
2. 低于拒绝线时直接拒答；
3. 中间灰区由 LLM 严格核验片段是否包含所需事实。

纯向量路径或重排功能关闭时，才回退到余弦相似度 + 词面覆盖规则。任何候选都不
满足时返回空列表，由 RAG Pipeline 进入 no_context 分支。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from app.config import settings
from app.core.retriever import RetrievedChunk
from app.llm import qwen
from app.llm.prompt_templates import build_evidence_judge_messages

logger = logging.getLogger(__name__)

_LATIN_TOKEN = re.compile(r"[a-zA-Z][a-zA-Z0-9_.+\-]{1,}")
_CJK_SEQUENCE = re.compile(r"[\u4e00-\u9fff]+")
_QUERY_FILLERS = (
    "请问", "麻烦", "帮我", "一下", "怎么样", "怎么", "如何", "什么",
    "哪些", "多少", "是否", "能否", "可以", "介绍", "告诉", "为什么",
    "的吗", "呢", "吗", "呀", "啊", "的", "是",
)


@dataclass(frozen=True)
class EvidenceDecision:
    accepted: bool
    reason: str
    supporting_chunks: int
    total_chunks: int
    max_similarity: float | None
    max_lexical_coverage: float
    max_rerank_score: float | None
    judge_used: bool

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "supporting_chunks": self.supporting_chunks,
            "total_chunks": self.total_chunks,
            "max_similarity": self.max_similarity,
            "max_lexical_coverage": self.max_lexical_coverage,
            "max_rerank_score": self.max_rerank_score,
            "judge_used": self.judge_used,
        }


def _query_terms(query: str) -> set[str]:
    """提取可解释的词面单位：英文术语 + 去口语填充词后的中文二元组。"""
    terms = {token.casefold() for token in _LATIN_TOKEN.findall(query)}
    for sequence in _CJK_SEQUENCE.findall(query):
        cleaned = sequence
        for filler in _QUERY_FILLERS:
            cleaned = cleaned.replace(filler, "")
        if not cleaned:
            continue
        if len(cleaned) <= 2:
            terms.add(cleaned)
        else:
            terms.update(cleaned[index:index + 2] for index in range(len(cleaned) - 1))
    return {term for term in terms if term.strip()}


def lexical_coverage(query: str, text: str) -> float:
    """查询关键单位在候选文本中的覆盖比例，范围 0～1。"""
    terms = _query_terms(query)
    if not terms:
        return 0.0
    haystack = text.casefold()
    return sum(term in haystack for term in terms) / len(terms)


def _semantic_score(chunk: RetrievedChunk) -> float | None:
    """优先取原始向量相似度；纯向量/重排路径可回退到可解释的 0～1 score。

    RRF 常见分值约 0.01～0.04，低于 0.2，因此不会被误当成余弦相似度。
    """
    if chunk.similarity is not None:
        return float(chunk.similarity)
    score = float(chunk.score)
    return score if 0.2 <= score <= 1.0 else None


def _observations(query: str, chunks: list[RetrievedChunk]) -> tuple[float | None, float]:
    similarities = [score for chunk in chunks if (score := _semantic_score(chunk)) is not None]
    lexical_scores = [lexical_coverage(query, chunk.text) for chunk in chunks]
    return max(similarities, default=None), max(lexical_scores, default=0.0)


def _decision(
    *,
    accepted: bool,
    reason: str,
    supporting_chunks: int,
    total_chunks: int,
    max_similarity: float | None,
    max_lexical_coverage: float,
    max_rerank_score: float | None = None,
    judge_used: bool = False,
) -> EvidenceDecision:
    return EvidenceDecision(
        accepted=accepted,
        reason=reason,
        supporting_chunks=supporting_chunks,
        total_chunks=total_chunks,
        max_similarity=max_similarity,
        max_lexical_coverage=max_lexical_coverage,
        max_rerank_score=max_rerank_score,
        judge_used=judge_used,
    )


def _rerank_scores(query: str, chunks: list[RetrievedChunk]) -> list[float]:
    """取得与原候选下标对齐的重排分；已有分数直接复用，避免重复调用。"""
    if all(chunk.rerank_score is not None for chunk in chunks):
        return [float(chunk.rerank_score) for chunk in chunks]
    ordered = qwen.rerank_texts(query, [chunk.text for chunk in chunks])
    scores: list[float | None] = [None] * len(chunks)
    for index, score in ordered:
        if 0 <= index < len(chunks):
            scores[index] = float(score)
    if any(score is None for score in scores):
        raise ValueError("重排响应未覆盖全部候选片段")
    return [float(score) for score in scores if score is not None]


def _parse_judge_output(output: str, candidate_count: int) -> list[int] | None:
    """解析核验 JSON，返回从 0 开始的支持片段下标；不可信输出返回 None。"""
    try:
        start, end = output.find("{"), output.rfind("}")
        data = json.loads(output[start:end + 1])
    except Exception:
        return None
    answerable = data.get("answerable")
    indexes = data.get("supporting_indexes")
    if not isinstance(answerable, bool) or not isinstance(indexes, list):
        return None
    if not answerable:
        return []
    parsed: list[int] = []
    for value in indexes:
        if isinstance(value, int) and 1 <= value <= candidate_count:
            parsed.append(value - 1)
    return list(dict.fromkeys(parsed)) or None


def _judge_gray_zone(
    query: str,
    chunks: list[RetrievedChunk],
    scores: list[float],
) -> list[int] | None:
    """核验重排灰区；返回原 chunks 下标，None 表示调用或输出不可信。"""
    limit = max(1, settings.evidence_judge_max_chunks)
    ranked_indexes = sorted(range(len(chunks)), key=lambda index: scores[index], reverse=True)[:limit]
    selected = [chunks[index].text for index in ranked_indexes]
    output = qwen.chat_completion(
        build_evidence_judge_messages(query, selected),
        model=settings.evidence_judge_model or settings.llm_model,
    )
    judged = _parse_judge_output(output, len(selected))
    if judged is None:
        return None
    return [ranked_indexes[index] for index in judged]


def _rule_fallback(
    query: str,
    chunks: list[RetrievedChunk],
    similarity_limit: float,
    lexical_limit: float,
    required: int,
) -> tuple[list[RetrievedChunk], EvidenceDecision]:
    """纯向量模式与测试桩使用的本地规则，不产生额外模型调用。"""
    supported: list[RetrievedChunk] = []
    for chunk in chunks:
        similarity = _semantic_score(chunk)
        coverage = lexical_coverage(query, chunk.text)
        if (similarity is not None and similarity >= similarity_limit) or coverage >= lexical_limit:
            supported.append(chunk)
    max_similarity, max_lexical = _observations(query, chunks)
    accepted = len(supported) >= required
    return (supported if accepted else []), _decision(
        accepted=accepted,
        reason="rule_supported" if accepted else "weak_evidence",
        supporting_chunks=len(supported),
        total_chunks=len(chunks),
        max_similarity=max_similarity,
        max_lexical_coverage=max_lexical,
    )


def filter_evidence(
    query: str,
    chunks: list[RetrievedChunk],
    *,
    min_similarity: float | None = None,
    min_lexical_coverage: float | None = None,
    min_supporting_chunks: int | None = None,
) -> tuple[list[RetrievedChunk], EvidenceDecision]:
    """返回支持证据及判定详情；门槛关闭时原样放行。"""
    if not chunks:
        return [], _decision(
            accepted=False,
            reason="no_candidates",
            supporting_chunks=0,
            total_chunks=0,
            max_similarity=None,
            max_lexical_coverage=0.0,
        )
    if not settings.evidence_gate_enabled:
        max_similarity, max_lexical = _observations(query, chunks)
        return chunks, _decision(
            accepted=True,
            reason="gate_disabled",
            supporting_chunks=len(chunks),
            total_chunks=len(chunks),
            max_similarity=max_similarity,
            max_lexical_coverage=max_lexical,
        )

    similarity_limit = (
        settings.evidence_min_similarity if min_similarity is None else min_similarity
    )
    lexical_limit = (
        settings.evidence_min_lexical_coverage
        if min_lexical_coverage is None
        else min_lexical_coverage
    )
    required = max(1, (
        settings.evidence_min_supporting_chunks
        if min_supporting_chunks is None
        else min_supporting_chunks
    ))

    # 真实混合检索结果至少有一个原始向量相似度；测试桩和纯向量路径走本地规则，
    # 避免单元测试依赖外部模型，也保留关闭混合检索时的兼容行为。
    use_rerank = settings.evidence_rerank_enabled and any(
        chunk.similarity is not None or chunk.rerank_score is not None for chunk in chunks
    )
    if not use_rerank:
        result, decision = _rule_fallback(
            query, chunks, similarity_limit, lexical_limit, required
        )
        _log_decision(decision)
        return result, decision

    max_similarity, max_lexical = _observations(query, chunks)
    try:
        scores = _rerank_scores(query, chunks)
        for chunk, score in zip(chunks, scores):
            chunk.rerank_score = score
    except Exception:
        logger.exception("证据重排失败")
        if not settings.evidence_fail_closed:
            result, decision = _rule_fallback(
                query, chunks, similarity_limit, lexical_limit, required
            )
            _log_decision(decision)
            return result, decision
        decision = _decision(
            accepted=False,
            reason="rerank_failed_closed",
            supporting_chunks=0,
            total_chunks=len(chunks),
            max_similarity=max_similarity,
            max_lexical_coverage=max_lexical,
        )
        _log_decision(decision)
        return [], decision

    max_rerank = max(scores, default=0.0)
    accept_line = settings.evidence_rerank_accept_score
    reject_line = settings.evidence_rerank_reject_score
    ranked_indexes = sorted(range(len(chunks)), key=lambda index: scores[index], reverse=True)

    if max_rerank >= accept_line:
        supporting_indexes = [index for index in ranked_indexes if scores[index] >= reject_line]
        reason = "rerank_supported"
        judge_used = False
    elif max_rerank < reject_line:
        supporting_indexes = []
        reason = "rerank_rejected"
        judge_used = False
    elif settings.evidence_judge_enabled:
        judge_used = True
        try:
            judged = _judge_gray_zone(query, chunks, scores)
        except Exception:
            logger.exception("证据灰区核验失败")
            judged = None
        if judged is None:
            if settings.evidence_fail_closed:
                supporting_indexes = []
                reason = "judge_failed_closed"
            else:
                supporting_indexes = [ranked_indexes[0]]
                reason = "judge_failed_open"
        else:
            supporting_indexes = judged
            reason = "judge_supported" if judged else "judge_rejected"
    else:
        supporting_indexes = []
        reason = "rerank_gray_rejected"
        judge_used = False

    supported = [chunks[index] for index in supporting_indexes]
    accepted = len(supported) >= required
    decision = _decision(
        accepted=accepted,
        reason=reason if accepted or not supporting_indexes else "insufficient_support",
        supporting_chunks=len(supported),
        total_chunks=len(chunks),
        max_similarity=max_similarity,
        max_lexical_coverage=max_lexical,
        max_rerank_score=max_rerank,
        judge_used=judge_used,
    )
    _log_decision(decision)
    return (supported if accepted else []), decision


def _log_decision(decision: EvidenceDecision) -> None:
    """统一记录可用于评测标定的证据判定信息。"""
    logger.info(
        "证据门槛：accepted=%s reason=%s support=%d/%d "
        "max_similarity=%s max_lexical=%.3f max_rerank=%s judge=%s",
        decision.accepted,
        decision.reason,
        decision.supporting_chunks,
        decision.total_chunks,
        f"{decision.max_similarity:.3f}" if decision.max_similarity is not None else "none",
        decision.max_lexical_coverage,
        f"{decision.max_rerank_score:.3f}" if decision.max_rerank_score is not None else "none",
        decision.judge_used,
    )
