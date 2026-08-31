"""入库 → 检索冒烟演示（需要 API key）：把测试夹具文档入库并检索验证。

用法：
    .venv/bin/python scripts/index_demo.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from app.config import settings  # noqa: E402
from app.core import chunker, document_loader, embedder, retriever  # noqa: E402

KB = "kb_default"
FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
DOCS = ["sample.md", "sample.txt"]


def main() -> int:
    if not settings.dashscope_api_key:
        print("[FAIL] 未配置 DASHSCOPE_API_KEY（先复制 .env.example 为 .env 并填写）")
        return 1

    # 1. 入库（幂等：同 doc_id 覆盖）
    total = 0
    for name in DOCS:
        path = FIXTURES / name
        doc_id = f"demo_{path.stem}"
        fmt, elements = document_loader.load(path)
        chunks = chunker.chunk(elements, doc_id, KB, name)
        n = embedder.embed_and_store(KB, doc_id, chunks)
        total += n
        print(f"入库 {name}：fmt={fmt}，{n} chunks")

    # 2. 检索演示
    for query in ["千问怎么定价？", "embedding 模型是什么？", "支持流式输出吗？"]:
        hits = retriever.retrieve(KB, query)
        print(f"\n查询：{query} → {len(hits)} 条命中")
        for h in hits:
            print(f"  [{h.score:.3f}] ({h.source_file} P{h.page}) {h.text[:50]}...")

    print(f"\n[OK] 共入库 {total} chunks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
