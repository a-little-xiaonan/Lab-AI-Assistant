"""P3 FAQ 模板与 Context Cache 四组消融评测。

示例：
    cd backend
    ../.venv/bin/python scripts/evaluation/eval_faq_cache.py \
      --kb-id kb_public --dataset ../docs/eval/faq-cache-qa.example.json

脚本不会创建或激活模板。只有已审核且已激活的 FAQ 模板才会进入 B/C/D 组；未命中
会如线上一样回退 RAG，并在结果中保留实际 answer_mode，避免把“尝试缓存”误报为命中。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.core import rag_pipeline  # noqa: E402


GROUPS = {
    "A_rag_baseline": {"faq_template_enabled": False, "context_cache_enabled": False, "faq_template_rollout_percent": 0},
    "B_template_no_cache": {"faq_template_enabled": True, "context_cache_enabled": False, "faq_template_rollout_percent": 100},
    "C_cache_warmup": {"faq_template_enabled": True, "context_cache_enabled": True, "faq_template_rollout_percent": 100},
    "D_cache_reuse": {"faq_template_enabled": True, "context_cache_enabled": True, "faq_template_rollout_percent": 100},
}


@contextmanager
def _temporary_settings(values: dict[str, object]):
    """评测期间临时替换开关，结束后无论成败均恢复进程内设置。"""
    before = {key: getattr(settings, key) for key in values}
    try:
        for key, value in values.items():
            setattr(settings, key, value)
        yield
    finally:
        for key, value in before.items():
            setattr(settings, key, value)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return round(ordered[index], 2)


def _answer_case(case: dict, kb_id: str, group: str) -> dict:
    """用 SSE 路径计量 TTFT；不记录问题和回答正文。"""
    started = time.monotonic()
    first_delta_ms: float | None = None
    done = None
    error = None
    try:
        for item in rag_pipeline.answer_stream(
            case["question"], kb_id, audience_scope=case.get("audience_scope", "guest"),
            include_diagnostics=True,
        ):
            if item["type"] == "delta" and first_delta_ms is None:
                first_delta_ms = round((time.monotonic() - started) * 1000, 2)
            if item["type"] == "done":
                done = item
    except Exception as exc:  # 评测须保留单题失败，继续生成其余样本。
        error = f"{type(exc).__name__}: {exc}"
    end_to_end_ms = round((time.monotonic() - started) * 1000, 2)
    text = done["full_text"] if done else ""
    required = case.get("expected_answer_contains", [])
    coverage = None if error else all(token in text for token in required)
    expected_route = case.get("expected_route")
    diagnostics = done.get("diagnostics", {}) if done else {}
    actual_route = (
        f"faq:{diagnostics['faq_template_code']}" if diagnostics.get("faq_template_code")
        else diagnostics.get("answer_mode", "error" if not done else "unknown")
    )
    route_correct = None
    if expected_route and not error:
        if group == "A_rag_baseline":
            route_correct = actual_route == "rag"
        elif expected_route == "rag_fallback":
            route_correct = actual_route == "rag"
        else:
            route_correct = actual_route == expected_route
    return {
        "case_id": case["id"],
        "group": group,
        "cache_state": "warmup" if group == "C_cache_warmup" else "reuse" if group == "D_cache_reuse" else "off",
        "expected_route": expected_route,
        "actual_route": actual_route,
        "route_correct": route_correct,
        "answer_key_covered": coverage,
        "ttft_ms": first_delta_ms,
        "end_to_end_ms": end_to_end_ms,
        "error": error,
        # Provider 的 cached_tokens/cache_hit 由适配器安全日志提供；本次请求未返回时保持 unknown。
        "cached_tokens": None,
        "cache_hit": None,
    }


def _summary(rows: list[dict]) -> dict:
    ttft = [row["ttft_ms"] for row in rows if row["ttft_ms"] is not None]
    total = len(rows)
    covered = [row["answer_key_covered"] for row in rows if row["answer_key_covered"] is not None]
    routes = [row["route_correct"] for row in rows if row["route_correct"] is not None]
    return {
        "samples": total,
        "errors": sum(row["error"] is not None for row in rows),
        "answer_key_coverage": round(sum(covered) / len(covered), 4) if covered else None,
        "route_accuracy": round(sum(routes) / len(routes), 4) if routes else None,
        "ttft_p50_ms": _percentile(ttft, 0.5),
        "ttft_p95_ms": _percentile(ttft, 0.95),
        "end_to_end_p50_ms": _percentile([row["end_to_end_ms"] for row in rows], 0.5),
        "provider_cache_hits_confirmed": sum(row["cache_hit"] is True for row in rows),
    }


def _markdown(report: dict) -> str:
    lines = ["# FAQ Cache P3 消融评测结果", "", "| 组别 | 样本 | 错误 | 要点覆盖率 | TTFT P50/P95 (ms) | Provider 确认命中 |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for group, summary in report["summaries"].items():
        coverage = "-" if summary["answer_key_coverage"] is None else f"{summary['answer_key_coverage']:.2%}"
        ttft = f"{summary['ttft_p50_ms'] or '-'} / {summary['ttft_p95_ms'] or '-'}"
        lines.append(f"| {group} | {summary['samples']} | {summary['errors']} | {coverage} | {ttft} | {summary['provider_cache_hits_confirmed']} |")
    lines.extend(["", "说明：C 仅用于建立缓存，D 仅表示复用同一前缀后的请求；必须在供应商用量字段出现 `cached_tokens` 后，才可声明真实缓存命中。", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 FAQ Cache P3 四组消融评测")
    parser.add_argument("--kb-id", required=True)
    parser.add_argument("--dataset", type=Path, default=ROOT / "docs/eval/faq-cache-qa.example.json")
    parser.add_argument("--split", choices=["faq_cache_dev", "faq_cache_holdout", "all"], default="all")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "docs/eval/results")
    args = parser.parse_args()

    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    cases = [case for case in dataset["cases"] if args.split == "all" or case.get("split") == args.split]
    if not cases:
        parser.error("所选 split 没有样本")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {"run_id": uuid4().hex, "dataset_version": dataset.get("version"), "kb_id": args.kb_id, "split": args.split, "groups": deepcopy(GROUPS), "rows": [], "summaries": {}}
    for group, switches in GROUPS.items():
        rows = []
        with _temporary_settings({**switches, "faq_template_public_only": True}):
            for case in cases:
                row = _answer_case(case, args.kb_id, group)
                rows.append(row)
                report["rows"].append(row)
        report["summaries"][group] = _summary(rows)
    stem = f"faq-cache-{report['run_id']}"
    json_path = args.output_dir / f"{stem}.json"
    md_path = args.output_dir / f"{stem}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_markdown(report), encoding="utf-8")
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
