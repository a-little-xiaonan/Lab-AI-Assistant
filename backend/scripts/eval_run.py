"""批量评估 RAG 回答质量（Phase 2-04）：跑样例集 → 输出回答/引用/耗时 → 人工三档打分。

用法：
    ../.venv/bin/python scripts/eval_run.py \
        [--samples docs/eval/qa-samples.md] [--output docs/eval/results-YYYY-MM-DD.md] \
        [--limit N] [--kb kb_default]

依赖：真实 LLM 调用（.env 配好 DASHSCOPE_API_KEY）；单条失败记 failed 继续，不中断。
输出：results markdown（含三档打分空表，人工填 1-3 分后存档，作 Phase 3 对比基线）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from app.config import ROOT  # noqa: E402
from app.core.rag_pipeline import answer  # noqa: E402
from app.llm.errors import LLMError  # noqa: E402


def load_samples(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"```json\n(.*?)\n```", text, re.S)
    if not m:
        raise SystemExit(f"样例集格式错误：{path}（需 fenced ```json 块）")
    return json.loads(m.group(1))


def run_one(q: str, kb_id: str) -> dict:
    start = time.monotonic()
    try:
        result = answer(q, kb_id)
        return {
            "status": "ok",
            "answer": result["answer"][:200],
            "sources": result["sources"],
            "elapsed": round(time.monotonic() - start, 1),
            "error": None,
        }
    except LLMError as exc:
        return {"status": "failed", "answer": "", "sources": [], "elapsed": round(time.monotonic() - start, 1), "error": f"{exc.code}: {exc.message}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "answer": "", "sources": [], "elapsed": round(time.monotonic() - start, 1), "error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 评估脚本（人工三档打分基线）")
    parser.add_argument("--samples", default=str(ROOT / "docs/eval/qa-samples.md"))
    parser.add_argument("--output", default=str(ROOT / f"docs/eval/results-{date.today():%Y-%m-%d}.md"))
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（默认全部）")
    parser.add_argument("--kb", default="kb_default")
    args = parser.parse_args()

    samples = load_samples(Path(args.samples))
    if args.limit:
        samples = samples[: args.limit]

    print(f"开始评估：{len(samples)} 条样例 → {args.output}")
    ok = failed = 0
    rows = []
    for s in samples:
        print(f"  [{s['id']}/{s['category']}] {s['question'][:40]}...", flush=True)
        r = run_one(s["question"], args.kb)
        ok += r["status"] == "ok"
        failed += r["status"] == "failed"
        rows.append((s, r))
        if r["status"] == "failed":
            print(f"    ✗ {r['error']}")
        else:
            print(f"    ✓ {r['elapsed']}s, sources={len(r['sources'])}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# 评估结果 {date.today():%Y-%m-%d}（Phase 2-04 基线）",
        "",
        f"- 样例集：{Path(args.samples).name}（{len(samples)} 条）",
        f"- 知识库：{args.kb}",
        f"- 通过：{ok} / 失败：{failed}",
        f"- 版本：Phase 2（SSE + 短期记忆 + 引用规范化）",
        "",
        "## 逐条结果",
        "",
        "| # | 类别 | 问题 | 状态 | 耗时 | 引用数 | 回答（截断 200 字） |",
        "|---|------|------|------|------|--------|--------------------|",
    ]
    for s, r in rows:
        q = s["question"].replace("|", "\\|")
        a = (r["answer"] or r["error"] or "").replace("\n", " ").replace("|", "\\|")[:200]
        lines.append(f"| {s['id']} | {s['category']} | {q} | {r['status']} | {r['elapsed']}s | {len(r['sources'])} | {a} |")
    lines += [
        "",
        "## 人工打分（1-3 分：1 差 / 2 中 / 3 好）",
        "",
        "| # | 引用正确性 | 回答准确性 | 告知明确性 | 备注 |",
        "|---|-----------|-----------|-----------|------|",
    ]
    for s, _ in rows:
        lines.append(f"| {s['id']} |  |  |  |  |")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n完成：{ok} 通过 / {failed} 失败，结果已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
