"""实验室招新 RAG 自动评测。

评测内容：
- 有答案题：Recall@5、MRR、标准要点覆盖率、有效来源精确率；
- 无答案题：证据门槛准确率、误放行率、拒答准确率；
- 工程指标：检索/生成/端到端平均耗时与 P95、失败率。

用法：
    ../.venv/bin/python scripts/evaluation/eval_recruitment.py --mode retrieval
    ../.venv/bin/python scripts/evaluation/eval_recruitment.py --mode full --limit 10

retrieval 模式不生成最终回答，适合快速标定门槛；full 模式会调用对话模型。
结果写入 data/eval，不进入 Git。
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import math
import uuid
import sys
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import ROOT  # noqa: E402
from app.core.retrieval import retriever  # noqa: E402
from app.core.retrieval.retriever import estimate_tokens  # noqa: E402
from app.core.retrieval.ranking.evidence_gate import filter_evidence  # noqa: E402
from app.core.rag_pipeline import answer  # noqa: E402
from app.models.database import KnowledgeBase  # noqa: E402
from app.store.db import SessionLocal  # noqa: E402
from app.services.operations.evaluation import (  # noqa: E402
    finish_run, git_commit, knowledge_snapshot, safe_config_snapshot, start_run,
)
from app.llm import qwen  # noqa: E402

REFUSAL_MARKERS = ("未找到", "没有相关", "无法找到", "资料不足", "未提供")


def _judge_answer(case: dict, result: dict) -> dict | None:
    """LLM Judge 只接收问题、标准要点、回答和引用片段；输出不可信则留待人工。"""
    payload = {
        "question": case["question"],
        "expected_points": case.get("expected_points", []),
        "answer": result.get("answer", ""),
        "citations": [item.get("snippet", "") for item in result.get("sources", [])],
    }
    messages = [
        {"role": "system", "content": "你是严格的RAG评测员。只基于输入评分，输出JSON，不要解释。字段 correctness、groundedness、citation_precision 均为0到1小数，reason不超过80字。"},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    try:
        text = qwen.chat_completion(messages)
        data = json.loads(text[text.find("{"):text.rfind("}") + 1])
        if not all(isinstance(data.get(key), (int, float)) and 0 <= data[key] <= 1
                   for key in ("correctness", "groundedness", "citation_precision")):
            return None
        return data
    except Exception:
        return None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def _point_coverage(answer_text: str, expected_points: list[list[str]]) -> float | None:
    if not expected_points:
        return None
    normalized = _normalize(answer_text)
    matched = sum(
        any(_normalize(alternative) in normalized for alternative in alternatives)
        for alternatives in expected_points
    )
    return matched / len(expected_points)


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def _average(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(len(ordered) * 0.95 + 0.9999) - 1))]


def _p50(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _resolve_kb_id(kb_id: str | None, kb_name: str) -> str:
    if kb_id:
        return kb_id
    with SessionLocal() as db:
        kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == kb_name))
    if kb is None:
        raise SystemExit(f"未找到知识库：{kb_name}")
    return kb.id


def _source_metrics(retrieved: list[dict], expected_sources: list[str]) -> tuple[bool, float, float]:
    if not expected_sources:
        return False, 0.0, 0.0
    names = [str(item.get("source_file", "")) for item in retrieved]
    relevant_ranks = [
        index for index, name in enumerate(names, 1) if name in expected_sources
    ]
    recall = bool(relevant_ranks)
    reciprocal_rank = 1.0 / min(relevant_ranks) if relevant_ranks else 0.0
    precision = (
        sum(name in expected_sources for name in names) / len(names) if names else 0.0
    )
    return recall, reciprocal_rank, precision


def _ndcg_at_5(retrieved: list[dict], expected_sources: list[str]) -> float | None:
    if not expected_sources:
        return None
    gains = [1.0 if str(item.get("source_file", "")) in expected_sources else 0.0 for item in retrieved[:5]]
    dcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))
    ideal = sum(1.0 / math.log2(index + 2) for index in range(min(5, len(set(expected_sources)))))
    return dcg / ideal if ideal else 0.0


def _run_retrieval(case: dict, kb_id: str) -> dict:
    started = time.monotonic()
    raw_chunks = retriever.retrieve(kb_id, case["question"])
    chunks, evidence = filter_evidence(case["question"], raw_chunks)
    elapsed = time.monotonic() - started
    return {
        "status": "ok",
        "answer": "",
        "sources": [],
        "retrieval_elapsed_seconds": elapsed,
        "generation_elapsed_seconds": 0.0,
        "total_elapsed_seconds": elapsed,
        "evidence": evidence.as_dict(),
        "retrieved": [
            {
                "rank": index,
                "chunk_id": chunk.chunk_id,
                "source_file": chunk.source_file,
                "score": float(chunk.score),
                "similarity": chunk.similarity,
                "rerank_score": chunk.rerank_score,
            }
            for index, chunk in enumerate(chunks, 1)
        ],
        "trace": {"original_query": case["question"], "final_candidates": [chunk.chunk_id for chunk in chunks]},
    }


def _run_full(case: dict, kb_id: str) -> dict:
    started = time.monotonic()
    result = answer(case["question"], kb_id, include_diagnostics=True)
    diagnostics = result["diagnostics"]
    return {
        "status": "ok",
        "answer": result["answer"],
        "sources": result["sources"],
        "retrieval_elapsed_seconds": diagnostics["retrieval_elapsed_seconds"],
        "generation_elapsed_seconds": diagnostics["generation_elapsed_seconds"],
        "total_elapsed_seconds": time.monotonic() - started,
        "evidence": diagnostics["evidence"],
        "retrieved": diagnostics["retrieved"],
        "trace": {"original_query": case["question"], "final_candidates": [item["chunk_id"] for item in diagnostics["retrieved"]]},
    }


def _score_case(case: dict, result: dict) -> dict:
    answerable = bool(case["answerable"])
    accepted = bool(result["evidence"]["accepted"])
    recall, reciprocal_rank, source_precision = _source_metrics(
        result["retrieved"], case.get("expected_sources", [])
    )
    coverage = (
        _point_coverage(result["answer"], case.get("expected_points", []))
        if result["answer"]
        else None
    )
    refusal = any(marker in result["answer"] for marker in REFUSAL_MARKERS)
    return {
        **result,
        "id": case["id"],
        "category": case["category"],
        "question": case["question"],
        "answerable": answerable,
        "gate_correct": accepted == answerable,
        "recall_at_5": recall if answerable else None,
        "reciprocal_rank": reciprocal_rank if answerable else None,
        "source_precision": source_precision if answerable else None,
        "ndcg_at_5": _ndcg_at_5(result["retrieved"], case.get("expected_sources", [])) if answerable else None,
        "answer_point_coverage": coverage,
        "refusal_correct": (refusal and not accepted) if not answerable and result["answer"] else None,
    }


def _summarize(rows: list[dict], mode: str) -> dict:
    successful = [row for row in rows if row["status"] == "ok"]
    answerable = [row for row in successful if row["answerable"]]
    no_answer = [row for row in successful if not row["answerable"]]
    point_scores = [
        row["answer_point_coverage"]
        for row in answerable
        if row["answer_point_coverage"] is not None
    ]
    refusal_scores = [
        row["refusal_correct"]
        for row in no_answer
        if row["refusal_correct"] is not None
    ]
    retrieval_times = [row["retrieval_elapsed_seconds"] for row in successful]
    total_times = [row["total_elapsed_seconds"] for row in successful]
    permission_rows = [row for row in successful if row["category"] in {"permission", "stale_conflict"}]
    judged = [row["judge"] for row in successful if row.get("judge")]
    return {
        "mode": mode,
        "sample_count": len(rows),
        "success_count": len(successful),
        "failure_count": len(rows) - len(successful),
        "retrieval_recall_at_5": (
            sum(bool(row["recall_at_5"]) for row in answerable) / len(answerable)
            if answerable else None
        ),
        "mrr": (
            statistics.mean(row["reciprocal_rank"] for row in answerable)
            if answerable else None
        ),
        "ndcg_at_5": _average([row["ndcg_at_5"] for row in answerable if row.get("ndcg_at_5") is not None]),
        "source_precision": (
            statistics.mean(row["source_precision"] for row in answerable)
            if answerable else None
        ),
        "evidence_gate_accuracy": (
            sum(bool(row["gate_correct"]) for row in successful) / len(successful)
            if successful else None
        ),
        "no_answer_false_accept_rate": (
            sum(bool(row["evidence"]["accepted"]) for row in no_answer) / len(no_answer)
            if no_answer else None
        ),
        "answerable_false_reject_rate": (
            sum(not bool(row["evidence"]["accepted"]) for row in answerable) / len(answerable)
            if answerable else None
        ),
        "evidence_judge_call_rate": (
            sum(bool(row["evidence"].get("judge_used")) for row in successful) / len(successful)
            if successful else None
        ),
        "average_supporting_chunks": _average(
            [float(row["evidence"]["supporting_chunks"]) for row in successful]
        ),
        "answer_point_coverage": _average(point_scores),
        "no_answer_refusal_accuracy": _average([float(value) for value in refusal_scores]),
        "retrieval_average_seconds": _average(retrieval_times),
        "retrieval_p50_seconds": _p50(retrieval_times),
        "retrieval_p95_seconds": _p95(retrieval_times),
        "end_to_end_average_seconds": _average(total_times),
        "end_to_end_p50_seconds": _p50(total_times),
        "end_to_end_p95_seconds": _p95(total_times),
        "permission_safety": (sum(not bool(row["evidence"]["accepted"]) for row in permission_rows) / len(permission_rows)) if permission_rows else None,
        "estimated_input_tokens": sum(estimate_tokens(row.get("question", "")) for row in successful),
        "estimated_output_tokens": sum(estimate_tokens(row.get("answer", "")) for row in successful),
        "judge_answer_correctness": _average([float(item["correctness"]) for item in judged]),
        "judge_groundedness": _average([float(item["groundedness"]) for item in judged]),
        "judge_citation_precision": _average([float(item["citation_precision"]) for item in judged]),
        "judge_pending_count": sum(1 for row in successful if row.get("answer") and not row.get("judge")),
    }


def _write_report(path: Path, kb_id: str, summary: dict, rows: list[dict], metadata: dict) -> None:
    lines = [
        f"# 实验室招新 RAG 评测报告（{datetime.now():%Y-%m-%d %H:%M}）",
        "",
        f"- 知识库：{kb_id}",
        f"- 模式：{summary['mode']}",
        f"- 运行 ID：{metadata['run_id']}",
        f"- 数据集版本/分区：{metadata['dataset_version']} / {metadata['split']}",
        f"- Git：{metadata.get('git_commit') or '-'}",
        f"- 知识库快照：{metadata['kb_snapshot']}",
        f"- 样例：{summary['sample_count']} 条，成功 {summary['success_count']} 条，失败 {summary['failure_count']} 条",
        f"- Recall@5：{_percent(summary['retrieval_recall_at_5'])}",
        f"- MRR：{summary['mrr'] if summary['mrr'] is not None else '-'}",
        f"- nDCG@5：{summary['ndcg_at_5'] if summary['ndcg_at_5'] is not None else '-'}",
        f"- 有效来源精确率：{_percent(summary['source_precision'])}",
        f"- 证据门槛准确率：{_percent(summary['evidence_gate_accuracy'])}",
        f"- 无答案误放行率：{_percent(summary['no_answer_false_accept_rate'])}",
        f"- 有答案误拒率：{_percent(summary['answerable_false_reject_rate'])}",
        f"- 灰区 LLM 核验调用率：{_percent(summary['evidence_judge_call_rate'])}",
        f"- 平均支持片段数：{summary['average_supporting_chunks'] if summary['average_supporting_chunks'] is not None else '-'}",
        f"- 回答要点覆盖率：{_percent(summary['answer_point_coverage'])}",
        f"- 无答案拒答准确率：{_percent(summary['no_answer_refusal_accuracy'])}",
        f"- 检索平均/P95：{summary['retrieval_average_seconds'] or 0:.2f}s / {summary['retrieval_p95_seconds'] or 0:.2f}s",
        f"- 检索 P50/P95：{summary['retrieval_p50_seconds'] or 0:.2f}s / {summary['retrieval_p95_seconds'] or 0:.2f}s",
        f"- 端到端平均/P95：{summary['end_to_end_average_seconds'] or 0:.2f}s / {summary['end_to_end_p95_seconds'] or 0:.2f}s",
        f"- 权限安全：{_percent(summary['permission_safety'])}",
        f"- Judge 正确性/有据性/引用精确率：{_percent(summary['judge_answer_correctness'])} / {_percent(summary['judge_groundedness'])} / {_percent(summary['judge_citation_precision'])}",
        "",
        "## 逐题结果",
        "",
        "| ID | 类别 | 可回答 | 门槛 | Recall@5 | MRR | 要点覆盖 | 耗时 | 问题 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        if row["status"] != "ok":
            lines.append(
                f"| {row['id']} | {row['category']} | - | - | - | - | - | - | {row['question']}（失败：{row['error']}） |"
            )
            continue
        lines.append(
            f"| {row['id']} | {row['category']} | {'是' if row['answerable'] else '否'} "
            f"| {'通过' if row['evidence']['accepted'] else '拒绝'} "
            f"| {_percent(row['recall_at_5'])} "
            f"| {row['reciprocal_rank'] if row['reciprocal_rank'] is not None else '-'} "
            f"| {_percent(row['answer_point_coverage'])} "
            f"| {row['total_elapsed_seconds']:.2f}s | {row['question']} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.with_suffix(".json").write_text(
        json.dumps({"metadata": metadata, "summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="实验室招新 RAG 自动评测")
    parser.add_argument(
        "--dataset", "--samples", dest="samples",
        default=str(ROOT / "docs/eval/recruitment-qa.json"),
    )
    parser.add_argument("--kb-id", default=None)
    parser.add_argument("--kb-name", default="实验室招新公开资料")
    parser.add_argument("--mode", choices=("retrieval", "full"), default="retrieval")
    parser.add_argument("--split", choices=("dev", "holdout", "all"), default="dev")
    parser.add_argument("--kb-snapshot", default="")
    parser.add_argument("--concurrency", type=int, default=1, help="预留参数；当前为避免触发模型限流按顺序执行")
    parser.add_argument("--judge-enabled", action="store_true", help="预留 LLM Judge 开关；失败时保留自动指标")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--ids",
        default="",
        help="只运行指定题号，英文逗号分隔，例如 F01,F02,N01",
    )
    parser.add_argument(
        "--output",
        default="",
    )
    args = parser.parse_args()

    cases = json.loads(Path(args.samples).read_text(encoding="utf-8"))
    if args.split != "all":
        cases = [case for case in cases if case.get("split", "dev") == args.split]
    if args.ids:
        case_map = {case["id"]: case for case in cases}
        selected_ids = [value.strip() for value in args.ids.split(",") if value.strip()]
        missing = [case_id for case_id in selected_ids if case_id not in case_map]
        if missing:
            raise SystemExit(f"评测题号不存在：{', '.join(missing)}")
        cases = [case_map[case_id] for case_id in selected_ids]
    if args.limit:
        cases = cases[:args.limit]
    if args.repeat > 1:
        cases = [{**case, "id": f"{case['id']}#r{repeat + 1}"}
                 for repeat in range(args.repeat) for case in cases]
    kb_id = _resolve_kb_id(args.kb_id, args.kb_name)
    run_id = args.run_id or f"eval_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    dataset_version = cases[0].get("dataset_version", "unversioned") if cases else "unversioned"
    kb_snapshot = args.kb_snapshot or knowledge_snapshot(kb_id)
    output = Path(args.output) if args.output else ROOT / "docs/eval/results" / f"{run_id}.md"
    metadata = {"run_id": run_id, "dataset_version": dataset_version, "split": args.split,
                "kb_snapshot": kb_snapshot, "git_commit": git_commit(),
                "config": safe_config_snapshot(), "judge_enabled": args.judge_enabled,
                "repeat": args.repeat,
                "human_review": {"status": "pending", "reviewer": None, "reviewed_at": None,
                                 "sample_rate_target": 0.1, "notes": []}}
    start_run(run_id, args.mode, dataset_version, args.split, kb_snapshot)
    runner = _run_full if args.mode == "full" else _run_retrieval
    rows: list[dict] = []
    for index, case in enumerate(cases, 1):
        print(f"[{index}/{len(cases)}] {case['id']} {case['question']}", flush=True)
        try:
            row = _score_case(case, runner(case, kb_id))
            if args.judge_enabled and args.mode == "full" and row.get("answer"):
                row["judge"] = _judge_answer(case, row)
            print(
                f"  gate={row['evidence']['accepted']} "
                f"recall={row['recall_at_5']} "
                f"sim={row['evidence']['max_similarity']} "
                f"lex={row['evidence']['max_lexical_coverage']:.3f} "
                f"rerank={row['evidence'].get('max_rerank_score')} "
                f"reason={row['evidence']['reason']}",
                flush=True,
            )
        except Exception as exc:
            row = {
                "id": case["id"],
                "category": case["category"],
                "question": case["question"],
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
            print(f"  失败：{row['error']}", flush=True)
        rows.append(row)

    summary = _summarize(rows, args.mode)
    _write_report(output, kb_id, summary, rows, metadata)
    finish_run(run_id, summary, output,
               "存在失败题" if summary["failure_count"] else None)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"报告：{output}")
    return 0 if summary["failure_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
