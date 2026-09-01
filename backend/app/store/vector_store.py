"""ChromaDB 薄封装：只暴露业务方法，不把 Collection/QueryResult 泄漏给上层。

- collection 命名：kb_{kb_id}_docs（对齐设计文档 §5；cosine 空间在创建时设定，
  后续想改需重建 collection——见附录 B）
- 幂等写入：同 doc_id 先删旧再插入
- 客户端单写：进程内锁保证写串行（MVP 并发上传场景少，够用）
- 迁移 Milvus/Qdrant 只需替换本文件
"""
from __future__ import annotations

import logging
import threading

import chromadb

from app.config import settings

logger = logging.getLogger(__name__)


class VectorStore:
    def __init__(self) -> None:
        self._client = chromadb.PersistentClient(path=str(settings.chroma_dir))
        self._lock = threading.RLock()
        self._collections: dict[str, object] = {}

    def _collection_name(self, kb_id: str, suffix: str = "docs") -> str:
        """collection 命名：kb_{kb_id}_{suffix}（docs / docs_new / docs_old）。"""
        return f"kb_{kb_id}_{suffix}"

    def _get_collection(self, kb_id: str, suffix: str = "docs"):
        name = self._collection_name(kb_id, suffix)
        with self._lock:
            coll = self._collections.get(name)
            if coll is None:
                coll = self._client.get_or_create_collection(
                    name=name, metadata={"hnsw:space": "cosine"}
                )
                self._collections[name] = coll
            return coll

    def add_chunks(self, kb_id: str, chunks: list, embeddings: list[list[float]],
                   suffix: str = "docs") -> None:
        """幂等写入：先删同 doc_id 的旧 chunk 再插入（suffix 供 reindex 写临时 collection）。"""
        if not chunks:
            return
        doc_id = chunks[0].doc_id
        with self._lock:
            coll = self._get_collection(kb_id, suffix)
            coll.delete(where={"doc_id": doc_id})  # 旧数据不存在时无副作用
            coll.add(
                ids=[f"{doc_id}_{c.chunk_index}" for c in chunks],
                documents=[c.text for c in chunks],
                embeddings=embeddings,
                metadatas=[c.metadata() for c in chunks],
            )

    def query(self, kb_id: str, query_embedding: list[float], top_k: int) -> list[tuple[str, str, dict, float]]:
        """返回 (chunk_id, text, metadata, distance) 列表，按距离升序（最近优先）。

        cosine 空间下 distance = 1 - 余弦相似度；相似度换算在 retriever 业务层。
        """
        coll = self._get_collection(kb_id)
        if coll.count() == 0:
            return []
        res = coll.query(query_embeddings=[query_embedding], n_results=top_k)
        return list(zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0]))

    def delete_document(self, kb_id: str, doc_id: str, suffix: str = "docs") -> None:
        coll = self._get_collection(kb_id, suffix)
        coll.delete(where={"doc_id": doc_id})

    def delete_collection(self, kb_id: str) -> None:
        """删除知识库全部向量：docs / docs_new / docs_old / memory（KB 级联删除与 reindex 共用）。"""
        for suffix in ("docs", "docs_new", "docs_old", "memory"):
            name = self._collection_name(kb_id, suffix)
            with self._lock:
                try:
                    self._client.delete_collection(name)
                except Exception:
                    pass  # 不存在即幂等
                self._collections.pop(name, None)

    # ----- 双 buffer 重索引（Phase 3-05）-----

    def create_temp_collection(self, kb_id: str) -> None:
        """双 buffer 起点：清理上次遗留的 docs_old/docs_new，重建 docs_new。"""
        with self._lock:
            for stale in ("docs_old", "docs_new"):
                name = self._collection_name(kb_id, stale)
                try:
                    self._client.delete_collection(name)
                except Exception:
                    pass
                self._collections.pop(name, None)
            name = self._collection_name(kb_id, "docs_new")
            coll = self._client.get_or_create_collection(
                name=name, metadata={"hnsw:space": "cosine"}
            )
            self._collections[name] = coll

    def swap_collections(self, kb_id: str) -> None:
        """双 buffer 切换：两次改名零删除 → 检索零中断窗口。

        序列（持锁；_get_collection 同锁 → 切换期间新查询阻塞毫秒级后拿到新句柄）：
        ① docs → docs_old（rename 保留 collection uuid，在途查询句柄仍有效）
        ② docs_new → docs（缓存键迁移）
        ③ docs_old 不删——留到下次 create_temp_collection 起点清理（在途查询永不撞删除）
        失败回滚：① 成功 ② 失败 → 尝试把 docs_old 改回 docs 恢复旧索引后抛异常。
        """
        with self._lock:
            old = self._get_collection(kb_id, "docs")
            new = self._get_collection(kb_id, "docs_new")
            if new.count() == 0:
                raise ValueError(f"临时 collection 为空，拒绝切换：{kb_id}")
            old.modify(name=self._collection_name(kb_id, "docs_old"))
            self._collections.pop(self._collection_name(kb_id, "docs"), None)
            self._collections[self._collection_name(kb_id, "docs_old")] = old
            try:
                new.modify(name=self._collection_name(kb_id, "docs"))
            except Exception:
                # 回滚：恢复旧索引
                try:
                    old.modify(name=self._collection_name(kb_id, "docs"))
                except Exception:
                    logger.exception("切换回滚失败（kb=%s），docs_old 将留待下次重建清理", kb_id)
                raise
            self._collections.pop(self._collection_name(kb_id, "docs_new"), None)
            self._collections[self._collection_name(kb_id, "docs")] = new

    def count(self, kb_id: str, suffix: str = "docs") -> int:
        return self._get_collection(kb_id, suffix).count()

    def get_doc_ids(self, kb_id: str, suffix: str = "docs") -> set[str]:
        """collection 内全部 doc_id 集合（reindex 一致性校验用）。"""
        coll = self._get_collection(kb_id, suffix)
        res = coll.get(include=["metadatas"])
        return {md["doc_id"] for md in res["metadatas"]}

    # ----- 长期记忆 collection（Phase 3-03；命名 kb_{kb_id}_memory，与 docs 隔离）-----

    def _get_memory_collection(self, kb_id: str):
        name = f"kb_{kb_id}_memory"
        return self._client.get_or_create_collection(
            name=name, metadata={"hnsw:space": "cosine"}
        )

    def add_memories(self, kb_id: str, entries: list[dict]) -> None:
        """写入记忆条目：entries=[{id, content, embedding, metadata}]。

        upsert：同 id（同 session 同内容 hash）覆盖，天然去重。
        """
        if not entries:
            return
        with self._lock:
            coll = self._get_memory_collection(kb_id)
            coll.upsert(
                ids=[e["id"] for e in entries],
                documents=[e["content"] for e in entries],
                embeddings=[e["embedding"] for e in entries],
                metadatas=[e["metadata"] for e in entries],
            )

    def query_memories(
        self, kb_id: str, query_embedding: list[float], top_k: int
    ) -> list[tuple[str, dict, float]]:
        """召回记忆：返回 (content, metadata, distance)，距离升序。"""
        coll = self._get_memory_collection(kb_id)
        if coll.count() == 0:
            return []
        res = coll.query(query_embeddings=[query_embedding], n_results=top_k)
        return list(zip(res["documents"][0], res["metadatas"][0], res["distances"][0]))

    def delete_session_memories(self, kb_id: str, session_id: str) -> int:
        """删除某会话贡献的记忆（清理入口），返回删除条数。"""
        coll = self._get_memory_collection(kb_id)
        res = coll.get(where={"session_id": session_id})
        ids = res["ids"]
        if ids:
            coll.delete(ids=ids)
        return len(ids)

    # ----- 用户级长期记忆（Phase 4-02；与旧 kb memory 分离）-----

    def _get_user_memory_collection(self):
        with self._lock:
            coll = self._collections.get("user_memories")
            if coll is None:
                coll = self._client.get_or_create_collection(
                    name="user_memories", metadata={"hnsw:space": "cosine"}
                )
                self._collections["user_memories"] = coll
            return coll

    def add_user_memories(self, entries: list[dict]) -> None:
        """写用户记忆。entries 元数据必须含 user_id；缺失时拒绝写入。"""
        if not entries:
            return
        if any(not e["metadata"].get("user_id") for e in entries):
            raise ValueError("用户记忆缺少 user_id，拒绝写入")
        with self._lock:
            coll = self._get_user_memory_collection()
            coll.upsert(
                ids=[e["id"] for e in entries],
                documents=[e["content"] for e in entries],
                embeddings=[e["embedding"] for e in entries],
                metadatas=[e["metadata"] for e in entries],
            )

    def query_user_memories(
        self, user_id: str, query_embedding: list[float], top_k: int
    ) -> list[tuple[str, dict, float]]:
        """强制 user_id 过滤，业务层不可绕过此方法查询个人记忆。"""
        coll = self._get_user_memory_collection()
        if coll.count() == 0:
            return []
        res = coll.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where={"$and": [{"user_id": {"$eq": user_id}}, {"status": {"$eq": "active"}}]},
        )
        return list(zip(res["documents"][0], res["metadatas"][0], res["distances"][0]))

    def delete_user_memory(self, memory_id: str) -> None:
        with self._lock:
            self._get_user_memory_collection().delete(ids=[memory_id])

    def delete_user_memories(self, user_id: str) -> int:
        with self._lock:
            coll = self._get_user_memory_collection()
            rows = coll.get(where={"user_id": user_id})
            ids = rows["ids"]
            if ids:
                coll.delete(ids=ids)
            return len(ids)


vector_store = VectorStore()
