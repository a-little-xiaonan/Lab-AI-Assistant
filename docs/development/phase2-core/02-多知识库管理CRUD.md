# Phase 2 · 02 多知识库管理 CRUD — 开发文档

> **所属阶段**：Phase 2 — 核心功能完善
> **路线图条目**：§11 Phase 2 第 2 项「多知识库管理 CRUD」
> **参考章节**：§4.3 知识库管理 · §5 向量数据库设计（collection 命名）· §8.2 知识库接口
> **前置依赖**：Phase 1-04（文档处理）、Phase 1-05（ChromaDB）
> **状态**：待开发

## 1. 目标与范围

知识库与文档的完整生命周期管理：创建 / 列表 / 详情 / 删除知识库，上传 / 列表 / 删除文档，配套统计接口。接口路径对齐 §8.2。

## 2. 任务拆解

- [x] 数据模型（SQLAlchemy 新增两张表）：
  - `KnowledgeBase`：id（`kb_` 前缀 UUID）、name、description、embedding_model、created_at
  - `Document`：id（`doc_` 前缀 UUID）、kb_id（外键）、filename、chunk_count、status、error_message、created_at
- [x] `app/api/knowledge_base.py` 路由（§8.2）：
  - `POST /api/knowledge-bases`（建库：校验名称唯一）
  - `GET /api/knowledge-bases`、`GET /api/knowledge-bases/{id}`（详情含文档列表与统计）
  - `DELETE /api/knowledge-bases/{id}`（事务性级联删除，见设计要点）
- [x] `app/api/documents.py` 路由（§8.2）：
  - `POST /api/knowledge-bases/{id}/documents`：上传 → 走 Phase 1-04 pipeline（解析→清洗→分块→向量化→入库）
  - `GET /api/knowledge-bases/{id}/documents`：文档列表（含 chunk 数、状态）
  - `DELETE /api/knowledge-bases/{id}/documents/{doc_id}`：删 chunk 向量 + 记录 + 原始文件
- [x] 文档处理改为**异步后台任务**：上传接口立即返回，处理在后台执行；`Document.status` 状态机：`processing → ready / failed`（失败带 error_message）
- [x] `GET /api/stats` 扩展：文档总数、chunk 总数、各库统计
- [x] 默认知识库迁移：`kb_default` 在建库接口中保留兼容（Phase 1 的隐式库显式化）

## 3. 设计要点

- **级联删除（事务性）**：删除知识库 = ① 删 SQLite 记录（KB + 其文档）→ ② 删 ChromaDB collection `kb_{id}_docs` → ③ 删 `data/uploads/{kb_id}/` 目录。顺序固定，任一步失败要可重试或记录半删状态
- **文档删除**：ChromaDB 按 `doc_id` 元数据过滤删除（`where={"doc_id": ...}`），随后删记录与文件
- **异步处理**：MVP 用 FastAPI 后台任务（`BackgroundTasks`）或 asyncio 任务即可，不引入 Celery；前端轮询文档状态（Phase 3-04 展示）
- **上传校验**：格式白名单放开（接受所有扩展名，由解析四级兜底链决定成败，见 Phase 1-04 §3.2；大小限制 50MB 保留）+ 同库同名去重
- 文档处理失败不污染库：失败时事务回滚已写入的向量（或先全量处理成功后再写库，二选一，保持一致）

## 4. 涉及文件

```
backend/app/
├── models/database.py   # + KnowledgeBase / Document 表
├── models/schemas.py    # + KB/Document 请求响应模型
├── api/knowledge_base.py # 知识库 CRUD（填充 Phase 1-02 占位）
├── api/documents.py     # 文档上传/列表/删除（替换 Phase 1-04 临时实现）
├── api/stats.py         # 统计扩展
├── store/vector_store.py # 补 delete_by_doc_id / collection 存在性判断
└── core/ 相关模块        # 文档处理编排为可后台调用的函数
```

## 5. 验收标准

- [x] curl 完成全流程：建库 → 上传 2 个文档 → 文档状态 processing → ready（chunk 数正确）→ 查询列表 → 删除文档 → 删除知识库
- [x] 删除知识库后：SQLite 无残留、ChromaDB 无该 collection、uploads 目录已清理
- [x] 上传损坏文件 → 文档状态 failed，error_message 可读，不影响库内其他文档
- [x] 重复上传同名文件 → 返回已存在提示（不重复入库）
- [x] 不存在的 kb_id 操作 → 404，错误结构统一

## 6. 风险与注意事项

- **破坏性操作**：删除知识库不可恢复，前端需二次确认（Phase 3-04 实现），后端也可加 `confirm` 参数
- **异步任务失败无感知**：后台处理失败只改状态，需在文档列表暴露状态字段，否则用户看不到失败原因
- **并发上传**：同一库同时上传多个文档，ChromaDB 写入需串行（Phase 1-05 进程内锁复用）；大文档处理时间长的用日志记录进度
