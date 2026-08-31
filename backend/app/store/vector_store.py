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

    def _get_collection(self, kb_id: str):
        name = f"kb_{kb_id}_docs"
        with self._lock:
            coll = self._collections.get(name)
            if coll is None:
                coll = self._client.get_or_create_collection(
                    name=name, metadata={"hnsw:space": "cosine"}
                )
                self._collections[name] = coll
            return coll

    def add_chunks(self, kb_id: str, chunks: list, embeddings: list[list[float]]) -> None:
        """幂等写入：先删同 doc_id 的旧 chunk 再插入。"""
        if not chunks:
            return
        doc_id = chunks[0].doc_id
        with self._lock:
            coll = self._get_collection(kb_id)
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

    def delete_document(self, kb_id: str, doc_id: str) -> None:
        coll = self._get_collection(kb_id)
        coll.delete(where={"doc_id": doc_id})

    def delete_collection(self, kb_id: str) -> None:
        """删除知识库全部向量（Phase 2-02 删除知识库时用）。"""
        name = f"kb_{kb_id}_docs"
        with self._lock:
            try:
                self._client.delete_collection(name)
            except Exception:
                logger.warning("删除 collection %s 失败（可能不存在）", name)
            self._collections.pop(name, None)

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

    def count(self, kb_id: str) -> int:
        return self._get_collection(kb_id).count()


vector_store = VectorStore()
