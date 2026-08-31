"""文档重新索引（Phase 3-05）：双 buffer 重建，检索全程不中断。

- 互斥：进程内任务 dict（kb_id → ReindexTask）；重复触发由 API 层判 409
- 双 buffer：新索引写入 docs_new 临时 collection → 全部成功 → swap_collections
  （两次改名零删除）→ 失败丢临时 collection，旧索引完好（绝不出现"索引一半"的中间态）
- 状态机（任务级）：running → done / failed；文档级 status：ready → reindexing → ready
  （reindexing 只是过程标记，失败/中断都回 ready——live 索引始终完好）
- 重建期间禁止向该 kb 上传（swap 会丢弃重建期间写入 live 的新文档；API 层 409）
- 重建期间删除文档允许：交换前做一致性校验（temp 的 doc_id 集合 == DB ready 文档集合），
  不匹配 → 失败丢 temp
- pipeline 复用：与首次上传完全相同的 load_and_chunk + embed_and_store（suffix 参数化），
  禁止复制逻辑
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.document_processing import load_and_chunk
from app.core.embedder import embed_and_store
from app.core.keyword_index import keyword_index
from app.core.retriever import estimate_tokens
from app.models.database import ChunkRecord, Document
from app.store.db import SessionLocal
from app.store.vector_store import vector_store

logger = logging.getLogger(__name__)


class ReindexTask:
    def __init__(self, kb_id: str, doc_id: str | None = None) -> None:
        self.kb_id = kb_id
        self.doc_id = doc_id
        self.status = "running"  # running / done / failed
        self.total = 0
        self.done = 0
        self.current_doc: str | None = None
        self.docs_before: int | None = None
        self.docs_after: int | None = None
        self.error_message: str | None = None
        self.started_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.finished_at: datetime | None = None


class ReindexManager:
    def __init__(self) -> None:
        self._tasks: dict[str, ReindexTask] = {}
        self._lock = threading.Lock()

    def is_running(self, kb_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(kb_id)
            return task is not None and task.status == "running"

    def get(self, kb_id: str) -> ReindexTask | None:
        with self._lock:
            return self._tasks.get(kb_id)

    def start(self, kb_id: str, doc_id: str | None = None) -> ReindexTask:
        """登记任务（run 由 BackgroundTasks 调度执行）。"""
        with self._lock:
            task = ReindexTask(kb_id, doc_id)
            self._tasks[kb_id] = task
            return task

    def run(self, kb_id: str, doc_id: str | None = None) -> None:
        """后台执行体：永不抛异常，失败状态化（task.failed + 可读错误）。

        单文档（doc_id）：直接重写 live collection（该文档旧向量先删后插，其余文档不受影响）；
        全库（缺省）：双 buffer（docs_new → swap），重建期间检索零中断。
        """
        task = self.get(kb_id)
        if task is None:
            return
        logger.info("开始重建索引：kb=%s doc=%s", kb_id, doc_id or "（全库）")
        db = SessionLocal()
        try:
            if doc_id:
                self._reindex_single(db, task, kb_id, doc_id)
            else:
                self._reindex_full(db, task, kb_id)
        except Exception as exc:
            db.rollback()
            logger.exception("重建失败（kb=%s）", kb_id)
            self._fail(task, str(exc))
            # 文档状态回 ready（live 索引与 DB 旧 chunk 记录仍一致）
            try:
                docs = db.scalars(
                    select(Document).where(Document.kb_id == kb_id, Document.status == "reindexing")
                )
                for d in docs:
                    d.status = "ready"
                db.commit()
            except Exception:
                logger.exception("重建失败后状态回滚失败（kb=%s）", kb_id)
        finally:
            db.close()

        # 成功交换后重建关键词索引（以新 chunk 文本全量重建最稳）
        if task.status == "done":
            try:
                keyword_index.rebuild_kb(kb_id)
            except Exception:
                logger.exception("关键词索引重建失败（kb=%s），下次检索可自愈", kb_id)

    # ----- 单文档重建：直接重写 live collection -----

    def _reindex_single(
        self, db, task: ReindexTask, kb_id: str, doc_id: str
    ) -> None:
        task.docs_before = vector_store.count(kb_id, "docs")
        doc = db.get(Document, doc_id)
        if doc is None or doc.kb_id != kb_id:
            raise RuntimeError(f"文档不存在：{doc_id}")
        task.total = 1
        task.current_doc = doc.filename
        doc.status = "reindexing"
        db.commit()
        try:
            _, chunks = load_and_chunk(doc)
            if chunks:
                # 全量成功才写入（embed 失败 → add 未执行 → 旧向量完好）
                embed_and_store(kb_id, doc_id, chunks, suffix="docs")
        except Exception as exc:
            raise RuntimeError(f"文档处理失败：{doc.filename}（{exc}）") from exc
        self._rewrite_doc_records(db, kb_id, doc_id, chunks)
        db.commit()
        task.done = 1
        task.docs_after = vector_store.count(kb_id, "docs")
        task.status = "done"
        task.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        logger.info("单文档重建完成：%s（chunks=%d）", doc.filename, len(chunks))

    # ----- 全库重建：双 buffer -----

    def _reindex_full(self, db, task: ReindexTask, kb_id: str) -> None:
        vector_store.create_temp_collection(kb_id)
        task.docs_before = vector_store.count(kb_id, "docs")

        targets = [
            d.id
            for d in db.scalars(
                select(Document).where(Document.kb_id == kb_id, Document.status == "ready")
            )
        ]
        task.total = len(targets)
        buffered: dict[str, list] = {}  # doc_id -> chunks（交换后统一写 DB）
        for did in targets:
            doc = db.get(Document, did)
            if doc is None or doc.kb_id != kb_id:
                continue
            task.current_doc = doc.filename
            doc.status = "reindexing"
            db.commit()
            try:
                _, chunks = load_and_chunk(doc)
                if chunks:
                    embed_and_store(kb_id, did, chunks, suffix="docs_new")
                buffered[did] = chunks
            except Exception as exc:
                logger.exception("重建文档失败：%s", doc.filename)
                doc.status = "ready"
                db.commit()
                raise RuntimeError(f"文档处理失败：{doc.filename}（{exc}）") from exc
            task.done += 1
            logger.info("重建进度 %d/%d：%s", task.done, task.total, doc.filename)

        # 一致性校验：temp 的 doc_id 集合 == DB 中"有向量"的文档集合
        # （ready + reindexing；failed/processing 无向量不参与；重建期间被删的文档
        #  不在集合内 → 校验失败丢 temp，防 swap 让已删文档"复活"）
        temp_ids = self._temp_doc_ids(kb_id)
        db_with_vectors = {
            d.id
            for d in db.scalars(
                select(Document).where(
                    Document.kb_id == kb_id,
                    Document.status.in_(["ready", "reindexing"]),
                )
            )
        }
        if temp_ids != db_with_vectors:
            raise RuntimeError(
                f"索引一致性校验失败（temp={len(temp_ids)} db={len(db_with_vectors)}），已丢弃临时索引"
            )

        # 交换（两次改名零删除，检索零中断）
        task.docs_after = vector_store.count(kb_id, "docs_new")
        vector_store.swap_collections(kb_id)

        # 交换后：单事务重写 ChunkRecord + 状态回 ready
        for did, chunks in buffered.items():
            self._rewrite_doc_records(db, kb_id, did, chunks)
        db.commit()
        task.status = "done"
        task.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        logger.info("全库重建完成：kb=%s chunks %s→%s", kb_id, task.docs_before, task.docs_after)

    def _temp_doc_ids(self, kb_id: str) -> set[str]:
        return vector_store.get_doc_ids(kb_id, "docs_new")

    def _rewrite_doc_records(
        self, db, kb_id: str, doc_id: str, chunks: list
    ) -> None:
        """重写单个文档的 chunk 明细（删旧行 + 插新行，与向量库一致）。"""
        db.execute(ChunkRecord.__table__.delete().where(ChunkRecord.doc_id == doc_id))
        for c in chunks:
            db.add(
                ChunkRecord(
                    id=f"{doc_id}_{c.chunk_index}",
                    doc_id=doc_id,
                    kb_id=kb_id,
                    chunk_index=c.chunk_index,
                    text=c.text,
                    char_length=len(c.text),
                    token_estimate=estimate_tokens(c.text),
                    page=c.page,
                    slide_number=c.slide_number,
                    sheet_name=c.sheet_name,
                    row_range=c.row_range,
                )
            )
        doc = db.get(Document, doc_id)
        if doc is not None:
            doc.status = "ready"
            doc.chunk_count = len(chunks)
            doc.error_message = None

    def _fail(self, task: ReindexTask, message: str) -> None:
        task.status = "failed"
        task.error_message = message
        task.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        # 临时 collection 不立即删：留待下次 create_temp_collection 起点幂等清理


reindex_manager = ReindexManager()  # 模块级单例
