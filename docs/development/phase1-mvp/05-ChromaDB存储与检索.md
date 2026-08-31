# Phase 1 · 05 ChromaDB 存储与检索 — 开发文档

> **所属阶段**：Phase 1 — MVP
> **路线图条目**：§11 Phase 1 第 5 项「ChromaDB 存储与检索」
> **参考章节**：§4.1 文档处理 Pipeline（向量化环节）· §5 向量数据库设计
> **前置依赖**：Phase 1-03（embedding）、Phase 1-04（chunk 产出）
> **状态**：待开发

## 1. 目标与范围

将 Phase 1-04 产出的 chunk 向量化后写入 ChromaDB（collection 命名对齐 §5），并提供检索接口（Top-K、相似度阈值、上下文 token 上限），验证持久化与幂等性。

## 2. 任务拆解

- [ ] 安装 `chromadb`（锁定版本）
- [ ] `app/store/vector_store.py`：封装 ChromaDB（薄封装，接口可迁移）
  - `get_or_create_collection(kb_id)` → collection 名 `kb_{kb_id}_docs`
  - `add_chunks(kb_id, chunks, embeddings)`：批量写入，携带 §4.1 元数据
  - `query(kb_id, query_embedding, top_k)` → `(chunk_text, score, metadata)`
  - `delete_document(kb_id, doc_id)` / `delete_collection(kb_id)`
  - persist 目录来自 `settings.CHROMA_PERSIST_DIR`（默认 `./data/chroma`）
- [ ] `app/core/embedder.py`：调用 `qwen.embed_texts`，分批向量化 + 入库，带进度回调（前端/日志用）
- [ ] `app/core/retriever.py`：检索接口，参数对齐 §5 建议值
  - `top_k = 5~10`（settings.RETRIEVAL_TOP_K，默认 5）
  - `similarity_threshold = 0.3` 过滤（低于阈值直接丢弃，返回空也不硬凑）
  - `max_context_tokens = 3000` 截断（按分数从高到低累积，超限截断）
- [ ] 幂等写入：相同 `doc_id` 重复入库 → 先删旧 chunk 再插入（或按 `(doc_id, chunk_index)` 覆盖）
- [ ] `scripts/index_demo.py`：冒烟 —— 2 个小文档入库 → 检索验证

## 3. 设计要点

- **薄封装原则**（附录 B）：`vector_store.py` 只暴露 6 个左右的方法，不把 ChromaDB 的对象（Collection/QueryResult）泄漏给上层；未来迁移 Milvus/Qdrant 只改这一个文件
- **collection 按 kb_id 隔离**：删除知识库 = `delete_collection`，天然清理干净（§5 命名 `kb_{kb_id}_docs`）
- **阈值过滤在 retriever 层**：ChromaDB 只负责返回 top-k 原始分数；是否可用由业务层判定（阈值、token 预算都是业务规则）
- **token 估算**：MVP 用粗估算（中文 ≈ 1 token/字，英文 ≈ 1 token/4 字符）即可，精确 token 计数后置（不影响正确性，只影响预算精度）
- 向量入库前**断言 1024 维**（与 Phase 1-03 的冒烟断言呼应），维度不一致直接报错，防止静默写脏数据
- 客户端单写：MVP 用进程内锁保证 collection 写入串行（并发上传场景少，够用）

## 4. 涉及文件

```
backend/app/store/vector_store.py   # ChromaDB 薄封装
backend/app/core/embedder.py        # 向量化 + 入库编排
backend/app/core/retriever.py       # 检索 + 阈值过滤 + token 截断
backend/scripts/index_demo.py       # 冒烟脚本
backend/requirements.txt            # 追加 chromadb
```

## 5. 验收标准

- [ ] 冒烟脚本：入库 2 个文档 → 检索 top-3，相关 chunk 分数 > 0.5 且顺序合理
- [ ] 无关问题检索 → 返回空（被 0.3 阈值过滤掉），不抛错
- [ ] 重启服务后检索结果仍在（持久化生效，`data/chroma/` 下有数据文件）
- [ ] 同一 doc_id 重新入库不产生重复向量（幂等）
- [ ] 删除 collection 后对应 kb 检索返回空

## 6. 风险与注意事项

- **ChromaDB 版本兼容性**：API 在 0.4/0.5 等版本间有差异，requirements.txt 锁定版本号
- **阈值 0.3 是建议值**：实际效果依赖 embedding 模型与文档内容，上线后按检索质量调整（附录 B 第三条风险）
- **持久化目录**：`CHROMA_PERSIST_DIR` 必须指向 gitignore 的目录，防止向量数据入库
- 检索性能在 MVP 规模（几千 chunk）无需优化；大规模场景的迁移方案已在附录 B 预留
