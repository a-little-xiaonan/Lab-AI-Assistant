"""解析预览（无需 API key）：看任意文件会被解析成什么样子、切出多少块。

用法：
    .venv/bin/python scripts/parse_preview.py 文件路径 [文件路径 ...]

输出每个 chunk 的索引、位置元数据与前 80 字，用于在没有 API key 时
直观验证解析与分块效果。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

from app.core import chunker, document_loader  # noqa: E402

KB = "kb_default"


def main() -> int:
    files = [Path(a) for a in sys.argv[1:]]
    if not files:
        print(__doc__)
        return 1
    for path in files:
        if not path.exists():
            print(f"[跳过] 文件不存在：{path}")
            continue
        try:
            fmt, elements = document_loader.load(path)
        except document_loader.UnsupportedFormatError as exc:
            print(f"[失败] {path.name}：{exc}")
            continue
        except document_loader.DocumentParseError as exc:
            print(f"[失败] {path.name}：{exc}")
            continue
        chunks = chunker.chunk(elements, "preview", KB, path.name)
        print(f"\n===== {path.name}（格式={fmt}，结构块={len(elements)}，chunk={len(chunks)}）=====")
        for c in chunks:
            loc = f"page={c.page}" if c.page is not None else (
                f"slide={c.slide_number}" if c.slide_number is not None else (
                    f"sheet={c.sheet_name}[{c.row_range}]" if c.sheet_name else "—"
                )
            )
            print(f"  [#{c.chunk_index:02d} {loc}] {c.text[:80].replace(chr(10), ' / ')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
