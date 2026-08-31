"""核心数据模型：解析中间结构（RawElement）与分块产物（Chunk）。

元数据字段命名是全链路契约：引用标注（Phase 2-04）、前端展示（Phase 3-04）只认这些名字。
字段按格式按需出现：PDF/DOCX 有 page；PPT 有 slide_number；Excel 有 sheet_name/row_range。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class RawElement:
    """文档解析中间结构：一段文本 + 它在原文中的位置信息（按格式按需填充）。"""

    text: str
    page: Optional[int] = None           # PDF / DOCX
    slide_number: Optional[int] = None   # PPT
    sheet_name: Optional[str] = None     # Excel
    row_range: Optional[str] = None      # Excel，如 "1-50"


@dataclass
class Chunk:
    """分块产物：进向量库的最小单元。"""

    doc_id: str
    kb_id: str
    source_file: str
    text: str
    chunk_index: int
    page: Optional[int] = None
    slide_number: Optional[int] = None
    sheet_name: Optional[str] = None
    row_range: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def metadata(self) -> dict:
        md = {
            "doc_id": self.doc_id,
            "kb_id": self.kb_id,
            "source_file": self.source_file,
            "chunk_index": self.chunk_index,
            "created_at": self.created_at,
        }
        if self.page is not None:
            md["page"] = self.page
        if self.slide_number is not None:
            md["slide_number"] = self.slide_number
        if self.sheet_name is not None:
            md["sheet_name"] = self.sheet_name
        if self.row_range is not None:
            md["row_range"] = self.row_range
        return md
