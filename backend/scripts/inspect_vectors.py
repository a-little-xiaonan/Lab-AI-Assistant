"""查看向量库内容：collection、chunk 数、向量维度与元数据示例。

用法：
    .venv/bin/python scripts/inspect_vectors.py [--limit 3]

向量存在 data/chroma/（ChromaDB 文件持久化：chroma.sqlite3 + 索引文件）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

import chromadb  # noqa: E402

from app.config import settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3, help="每个 collection 打印几条示例")
    args = parser.parse_args()

    client = chromadb.PersistentClient(path=str(settings.chroma_dir))
    collections = client.list_collections()
    if not collections:
        print("向量库为空（data/chroma 下没有 collection）")
        return 0

    print(f"向量库目录：{settings.chroma_dir}")
    print(f"collection 列表：{[c.name for c in collections]}\n")

    for coll in collections:
        c = client.get_collection(coll.name)
        n = c.count()
        print(f"===== {coll.name}（{n} chunks）=====")
        if n == 0:
            continue
        res = c.get(limit=args.limit, include=["documents", "metadatas", "embeddings"])
        for i, (doc, md, emb) in enumerate(
            zip(res["documents"], res["metadatas"], res["embeddings"])
        ):
            loc = f"P{md.get('page')}" if md.get("page") else (
                f"slide{md.get('slide_number')}" if md.get("slide_number") else (
                    f"{md.get('sheet_name')}[{md.get('row_range')}]" if md.get("sheet_name") else "—"
                )
            )
            print(f"  #{i+1} {md.get('source_file')} {loc} chunk_index={md.get('chunk_index')}")
            print(f"      维度={len(emb)} 前 8 维：{[round(float(x), 4) for x in emb[:8]]}")
            print(f"      文本：{doc[:70].replace(chr(10), ' / ')}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
