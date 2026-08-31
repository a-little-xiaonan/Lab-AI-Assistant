"""分块器纯逻辑测试（无需 API key）。"""
import pytest

from app.core.chunker import fixed_split
from app.core.models import RawElement


def test_short_text_kept_whole():
    text = "这是很短的一段文本。" * 5  # 20 字
    assert fixed_split(text) == [text]


def test_paragraph_split_all_chunks_within_size():
    text = "\n\n".join(f"第{i}段。" + "内容" * 100 for i in range(20))
    chunks = fixed_split(text, size=512, overlap=64)
    assert len(chunks) > 1
    assert all(len(c) <= 512 for c in chunks)


def test_overlap_between_adjacent_chunks():
    # 相邻块之间应保留 64 字重叠
    text = "\n\n".join(f"段落{i}。" + "内容" * 80 for i in range(15))
    chunks = fixed_split(text, size=512, overlap=64)
    for i in range(len(chunks) - 1):
        assert chunks[i + 1].startswith(chunks[i][-64:]) or chunks[i][-64:] in chunks[i + 1][:128]


def test_sentence_boundary_preferred():
    # 无换行的长文本：优先在句子标点处切，块尾应落在句末标点（除最后一块）
    sentences = [f"这是第{i}个完整的中文句子，用来测试分块时的句子边界。" for i in range(60)]
    text = "".join(sentences)
    chunks = fixed_split(text, size=512, overlap=64)
    assert len(chunks) > 1
    for c in chunks[:-1]:
        assert c.endswith(("。", "！", "？", "；", "；"))


def test_char_fallback_no_infinite_loop():
    text = "X" * 3000  # 无任何分隔符
    chunks = fixed_split(text, size=512, overlap=64)
    assert len(chunks) >= 5
    assert all(len(c) <= 512 for c in chunks)


def test_empty_text():
    assert fixed_split("") == []


def test_chunk_metadata_and_no_cross_structure():
    from app.core.chunker import chunk

    elements = [
        RawElement(text="甲" * 30, page=1),
        RawElement(text="乙" * 30, page=2),
    ]
    chunks = chunk(elements, doc_id="doc_1", kb_id="kb_default", source_file="a.md", size=512, overlap=64)
    assert len(chunks) == 2
    assert chunks[0].page == 1
    assert chunks[1].page == 2
    assert chunks[0].chunk_index == 0
    assert chunks[1].chunk_index == 1
    assert "乙" not in chunks[0].text
    assert "甲" not in chunks[1].text
    md = chunks[0].metadata()
    assert md["doc_id"] == "doc_1"
    assert md["source_file"] == "a.md"
    assert md["page"] == 1
    assert "slide_number" not in md  # 按需字段，无则省略


def test_overlap_never_crosses_structure_blocks():
    from app.core.chunker import chunk

    # 两个结构块都要被切碎：块间不得有内容交叉
    elements = [
        RawElement(text="\n\n".join(f"甲甲甲甲甲{i}。" for i in range(30)), page=1),
        RawElement(text="\n\n".join(f"乙乙乙乙乙{i}。" for i in range(30)), page=2),
    ]
    chunks = chunk(elements, "doc_2", "kb_default", "b.md", size=100, overlap=20)
    texts = [c.text for c in chunks]
    for t in texts:
        assert not ("甲" in t and "乙" in t)
    # 页码归属正确
    for c in chunks:
        assert (c.page == 1 and "甲" in c.text) or (c.page == 2 and "乙" in c.text)
