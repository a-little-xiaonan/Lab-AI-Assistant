"""进程内 BM25 关键词索引（Phase 3-06）：混合检索的关键词侧。

- 数据源：ChunkRecord 表（SQL 可查询副本，与 ChromaDB 双写同事务）；全量重建秒级
- 每个知识库一个 _KbIndex：chunk_ids/tokenized 对齐（jieba 分词缓存）+ texts/metas
  （关键词独有命中要拼 RetrievedChunk，meta 含 source_file/page 等位置信息）+ bm25
  （惰性重建：变更后置 None，下次 search 重建，避免增删时反复全文分词）
- 线程安全：写持 RLock；search 在锁外只调 bm25.get_scores()（纯函数）
- jieba 自定义词典：settings.keyword_jieba_dict 在 _ensure_ready 时一次性 load_userdict
- 失败语义：索引是缓存、DB 是真相；任何构建/同步异常 → 日志 + 置空该 kb，检索退化纯向量
"""
from __future__ import annotations

import logging
import threading

import jieba
from rank_bm25 import BM25Okapi
from sqlalchemy import select

from app.config import settings
from app.models.database import ChunkRecord, Document
from app.store.db import SessionLocal

logger = logging.getLogger(__name__)

jieba.setLogLevel(logging.WARNING)  # 压掉 jieba 前缀词典加载日志


def _meta_from_chunk(doc_id: str, c) -> dict:
    """从 Chunk 对象（core/models.py）构造索引元数据（与 ChromaDB 的 metadata() 对齐）。"""
    return {
        "doc_id": doc_id,
        "chunk_index": c.chunk_index,
        "source_file": c.source_file,
        "page": c.page,
        "slide_number": c.slide_number,
        "sheet_name": c.sheet_name,
        "row_range": c.row_range,
    }


def _meta_from_row(row) -> dict:
    """从 ChunkRecord 行构造索引元数据。"""
    return {
        "doc_id": row.doc_id,
        "chunk_index": row.chunk_index,
        "source_file": row.source_file if hasattr(row, "source_file") else row.doc_id,
        "page": row.page,
        "slide_number": row.slide_number,
        "sheet_name": row.sheet_name,
        "row_range": row.row_range,
    }


class _KbIndex:
    """单个知识库的 BM25 索引（语料 + 惰性 BM25 对象）。"""

    def __init__(self) -> None:
        self.chunk_ids: list[str] = []
        self.tokenized: list[list[str]] = []
        self.texts: dict[str, str] = {}
        self.metas: dict[str, dict] = {}
        self.bm25: BM25Okapi | None = None

    def rebuild_bm25(self) -> None:
        """重建 BM25 对象（idf 在构造时算死；纯 CPU 快，jieba 分词才是大头所以已缓存）。"""
        self.bm25 = BM25Okapi(self.tokenized) if self.tokenized else None


class KeywordIndex:
    def __init__(self) -> None:
        self._kbs: dict[str, _KbIndex] = {}
        self._filenames: dict[str, dict[str, str]] = {}  # kb_id -> {doc_id: filename}
        self._lock = threading.RLock()
        self._ready = False

    # ----- 构建 -----

    def _ensure_ready(self) -> None:
        """懒构建：首次检索前全量重建（幂等，持锁）。"""
        with self._lock:
            if self._ready:
                return
            if settings.keyword_jieba_dict:
                path = settings.keyword_jieba_dict
                if not path.startswith("/"):
                    from app.config import ROOT

                    path = str(ROOT / path)
                try:
                    jieba.load_userdict(path)
                    logger.info("jieba 自定义词典已加载：%s", path)
                except Exception:
                    logger.exception("jieba 自定义词典加载失败：%s", path)
            total = self.rebuild_all()
            self._ready = True
            logger.info("关键词索引全量重建完成：%d chunks", total)

    def rebuild_all(self) -> int:
        """全量重建（从 ChunkRecord + documents 表），返回 chunk 总数。"""
        with self._lock:
            self._kbs.clear()
            self._filenames.clear()
            total = 0
            db = SessionLocal()
            try:
                docs = db.execute(select(Document.id, Document.kb_id, Document.filename)).all()
                for did, dkb, fname in docs:
                    self._filenames.setdefault(dkb, {})[did] = fname
                rows = db.scalars(select(ChunkRecord).order_by(ChunkRecord.kb_id, ChunkRecord.id))
                for row in rows.yield_per(2000):
                    kb = self._kbs.setdefault(row.kb_id, _KbIndex())
                    kb.chunk_ids.append(row.id)
                    kb.tokenized.append(jieba.lcut(row.text))
                    kb.texts[row.id] = row.text
                    md = _meta_from_row(row)
                    md["source_file"] = self._filenames.get(row.kb_id, {}).get(
                        row.doc_id, row.doc_id
                    )
                    kb.metas[row.id] = md
                    total += 1
            finally:
                db.close()
            for kb in self._kbs.values():
                kb.rebuild_bm25()
            return total

    def rebuild_kb(self, kb_id: str) -> None:
        """单库重建（reindex 交换后调用：索引必须以新 chunk 文本重建）。"""
        with self._lock:
            self._ready = True
            self._kbs.pop(kb_id, None)
            self._filenames.pop(kb_id, None)
            db = SessionLocal()
            try:
                docs = db.execute(
                    select(Document.id, Document.filename).where(Document.kb_id == kb_id)
                ).all()
                self._filenames[kb_id] = {d[0]: d[1] for d in docs}
                rows = db.scalars(
                    select(ChunkRecord).where(ChunkRecord.kb_id == kb_id).order_by(ChunkRecord.id)
                )
                kb = _KbIndex()
                for row in rows:
                    kb.chunk_ids.append(row.id)
                    kb.tokenized.append(jieba.lcut(row.text))
                    kb.texts[row.id] = row.text
                    md = _meta_from_row(row)
                    md["source_file"] = self._filenames[kb_id].get(row.doc_id, row.doc_id)
                    kb.metas[row.id] = md
                kb.rebuild_bm25()
                self._kbs[kb_id] = kb
            finally:
                db.close()

    # ----- 增删同步（索引是缓存，失同步可被全量重建治愈；异常由调用方 try/except）-----

    def add_document(self, kb_id: str, doc_id: str, chunks: list) -> None:
        """追加文档的 chunks（replace 语义：先移除该 doc_id 旧条目再追加）。"""
        if not chunks:
            return
        with self._lock:
            self._ensure_ready()
            kb = self._kbs.setdefault(kb_id, _KbIndex())
            self._filenames.setdefault(kb_id, {})[doc_id] = chunks[0].source_file
            # 移除旧条目（同 doc_id 重新处理时）
            drop = {cid for cid in kb.chunk_ids if cid.startswith(f"{doc_id}_")}
            if drop:
                keep = [
                    (cid, toks)
                    for cid, toks in zip(kb.chunk_ids, kb.tokenized)
                    if cid not in drop
                ]
                kb.chunk_ids = [k[0] for k in keep]
                kb.tokenized = [k[1] for k in keep]
                for cid in drop:
                    kb.texts.pop(cid, None)
                    kb.metas.pop(cid, None)
            for c in chunks:
                chunk_id = f"{doc_id}_{c.chunk_index}"
                kb.chunk_ids.append(chunk_id)
                kb.tokenized.append(jieba.lcut(c.text))
                kb.texts[chunk_id] = c.text
                kb.metas[chunk_id] = _meta_from_chunk(doc_id, c)
            kb.rebuild_bm25()

    def remove_document(self, kb_id: str, doc_id: str) -> None:
        with self._lock:
            kb = self._kbs.get(kb_id)
            if kb is None:
                return
            drop = {cid for cid in kb.chunk_ids if cid.startswith(f"{doc_id}_")}
            if not drop:
                return
            keep = [
                (cid, toks)
                for cid, toks in zip(kb.chunk_ids, kb.tokenized)
                if cid not in drop
            ]
            kb.chunk_ids = [k[0] for k in keep]
            kb.tokenized = [k[1] for k in keep]
            for cid in drop:
                kb.texts.pop(cid, None)
                kb.metas.pop(cid, None)
            kb.rebuild_bm25()
            self._filenames.get(kb_id, {}).pop(doc_id, None)

    def remove_kb(self, kb_id: str) -> None:
        with self._lock:
            self._kbs.pop(kb_id, None)
            self._filenames.pop(kb_id, None)

    # ----- 检索 -----

    def search(self, kb_id: str, query: str, top_k: int) -> list[tuple[str, float]]:
        """BM25 检索：返回 [(chunk_id, score)] 降序。索引不可用返回空（退化纯向量）。"""
        try:
            self._ensure_ready()
        except Exception:
            logger.exception("关键词索引不可用（kb=%s），本次跳过关键词侧", kb_id)
            return []
        kb = self._kbs.get(kb_id)
        if kb is None or kb.bm25 is None:
            return []
        tokens = jieba.lcut(query)
        if not tokens:
            return []
        scores = kb.bm25.get_scores(tokens)
        ranked = sorted(zip(kb.chunk_ids, scores), key=lambda p: p[1], reverse=True)
        return [(cid, s) for cid, s in ranked if s > 0][:top_k]

    def get_meta(self, kb_id: str, chunk_id: str) -> dict | None:
        kb = self._kbs.get(kb_id)
        return kb.metas.get(chunk_id) if kb else None

    def get_text(self, kb_id: str, chunk_id: str) -> str | None:
        kb = self._kbs.get(kb_id)
        return kb.texts.get(chunk_id) if kb else None


keyword_index = KeywordIndex()  # 模块级单例
