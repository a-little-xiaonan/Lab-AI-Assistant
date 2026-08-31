"""两阶段分块：① 结构分块（边界由格式决定，在各格式加载器中产出结构元素）
→ ② 固定分块 fixed_split（512/64，分隔符层级递归切分）。

约定：
- 结构块 ≤ 512：整块保留不再切
- 相邻块之间保留 overlap=64 字重叠；overlap 永不跨结构块（块边界 = 元数据边界）
- 中文句子优先不被切断（句子标点层级在换行之后、字符硬切之前）
"""
from __future__ import annotations

import logging

from app.config import settings
from app.core.models import Chunk, RawElement

logger = logging.getLogger(__name__)

# 分隔符层级：L1 空行（段落）→ L2 换行（行）→ L3 句子标点 → L4 字符兜底
_SEP_LEVELS = ["\n\n", "\n"]
_SENTENCE_ENDS = "。！？!?；;”』"


def structural_split(elements: list[RawElement], _fmt: str | None = None) -> list[RawElement]:
    """① 结构分块：结构边界已在各格式加载器按格式产出（页内段落 / 标题节 /
    slide / sheet 行区块 / 段落）。此处兜底整理：丢弃空元素。"""
    return [el for el in elements if el.text and el.text.strip()]


def _split_sentences(text: str) -> list[str]:
    parts = re_split(text)
    return [p for p in parts if p.strip()]


def re_split(text: str) -> list[str]:
    """按句子标点切分，标点保留在前半句。"""
    import re

    return re.split(f"(?<=[{_SENTENCE_ENDS}])", text)


def _split_small(text: str, size: int) -> list[str]:
    """把 text 递归切成 ≤size 的片段，优先在高层级分隔符/句子边界切。"""
    if len(text) <= size:
        return [text] if text else []
    for sep in _SEP_LEVELS:
        if sep in text:
            pieces: list[str] = []
            for part in [p for p in text.split(sep) if p.strip()]:
                pieces.extend(_split_small(part, size))
            return pieces
    if any(c in text for c in _SENTENCE_ENDS):
        pieces = []
        for part in [p for p in re_split(text) if p.strip()]:
            pieces.extend(_split_small(part, size))
        return pieces
    # L4 字符兜底（避免无限递归）
    return [text[i : i + size] for i in range(0, len(text), size)]


def fixed_split(text: str, size: int = 512, overlap: int = 64) -> list[str]:
    """② 固定分块：结构块内按分隔符层级递归切到 ≤size，相邻块保留 overlap 重叠。"""
    parts = _split_small(text, size)
    if len(parts) <= 1:
        return parts
    chunks: list[str] = []
    current = ""
    for part in parts:
        if not current:
            current = part
        elif len(current) + len(part) <= size:
            current += part
        else:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            current = tail + part if len(part) <= size - overlap else part
    if current:
        chunks.append(current)
    return chunks


def chunk(
    elements: list[RawElement],
    doc_id: str,
    kb_id: str,
    source_file: str,
    size: int | None = None,
    overlap: int | None = None,
) -> list[Chunk]:
    """结构块 → 固定分块 → Chunk 列表（chunk_index 按文档顺序编号）。"""
    size = size if size is not None else settings.chunk_size
    overlap = overlap if overlap is not None else settings.chunk_overlap
    chunks: list[Chunk] = []
    index = 0
    for el in structural_split(elements):
        for seg in fixed_split(el.text, size, overlap):
            chunks.append(
                Chunk(
                    doc_id=doc_id,
                    kb_id=kb_id,
                    source_file=source_file,
                    text=seg,
                    chunk_index=index,
                    page=el.page,
                    slide_number=el.slide_number,
                    sheet_name=el.sheet_name,
                    row_range=el.row_range,
                )
            )
            index += 1
    return chunks
