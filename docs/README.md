# 项目目录与代码入口

本文是 RAG AI Assistant 的项目级导航页。第一次接触项目时，建议先看“核心调用链”，再按任务查找对应目录，不必从头阅读所有文件。

## 快速入口

- [项目说明与启动方式](../README.md)
- [总体技术设计](RAG-AI-Assistant-技术设计文档.md)
- [开发路线图索引](development/README.md)
- [系统优化实施总方案](design/实验室招新助手-系统优化实施总方案.md)
- [用户、知识库与记忆关系](design/用户-知识库-记忆关系设计.md)
- [文档数据治理与生命周期](design/文档数据治理与生命周期技术方案.md)
- [复杂问题拆解与定向检索](design/复杂问题拆解与意图定向检索实现方案.md)
- [轻量 Agent 与工具调用设计（待确认）](design/轻量Agent与工具调用设计方案.md)
- [招新引导与反馈闭环](design/新生引导与回答反馈闭环技术方案.md)
- [RAG 评测说明](eval/实验室招新RAG评测说明.md)

## 顶层目录

```text
Lab-AI-Assistant/
├── backend/                    FastAPI 后端、数据库迁移、任务脚本和测试
│   ├── app/                    后端应用源码
│   ├── alembic/                SQL 数据库版本迁移
│   ├── scripts/                管理、评测、知识维护和冒烟脚本
│   ├── tests/                  后端自动化测试
│   └── worker.py               RQ 后台任务 Worker 入口
├── frontend/                   Vue 3 + TypeScript 前端
│   └── src/                    页面、组件、状态和 API 客户端
├── docs/                       设计、开发路线图、评测资料和本导航
├── data/                       本地运行数据，不应作为源码提交
├── .env.example                环境变量模板
├── retrieval_topics.example.json  检索主题配置示例
├── term_aliases.example.json   术语别名配置示例
└── start.sh                    本地服务启动脚本
```

`data/` 中包含上传文件、ChromaDB、评测结果和日志。排查运行结果时可以查看，理解或修改业务代码时通常不从这里开始。

## 后端入口

### 应用启动与配置

| 路径 | 职责 |
| --- | --- |
| [`backend/app/main.py`](../backend/app/main.py) | FastAPI 应用入口；初始化数据库、恢复任务、注册中间件和总路由 |
| [`backend/app/config.py`](../backend/app/config.py) | 所有环境变量和功能开关的统一入口 |
| [`backend/app/api/router.py`](../backend/app/api/router.py) | 聚合所有业务路由并统一挂载到 `/api` |
| [`backend/app/middleware/request_context.py`](../backend/app/middleware/request_context.py) | 请求 ID 与日志上下文 |
| [`backend/app/api/errors.py`](../backend/app/api/errors.py) | API 业务异常及统一错误结构 |

### API 层

API 层只处理 HTTP 输入输出、依赖注入和权限入口，复杂算法放在 `core/`，跨接口业务放在 `services/`。

| 目录 | 主要接口 |
| --- | --- |
| [`api/conversation/`](../backend/app/api/conversation/) | 聊天、SSE、会话、记忆和回答反馈 |
| [`api/identity/`](../backend/app/api/identity/) | 注册登录、用户管理和角色申请 |
| [`api/knowledge/`](../backend/app/api/knowledge/) | 知识库、文档上传、版本审核和发布 |
| [`api/operations/`](../backend/app/api/operations/) | 审计日志、评测运行和后台任务管理 |
| [`api/system/`](../backend/app/api/system/) | 健康检查和系统统计 |

### RAG 与文档核心

| 路径 | 职责 |
| --- | --- |
| [`core/rag_pipeline.py`](../backend/app/core/rag_pipeline.py) | RAG 总编排：历史、检索、证据过滤、Prompt、生成与输出净化 |
| [`core/documents/parsing/`](../backend/app/core/documents/parsing/) | 文档加载、清洗、结构模型和切分 |
| [`core/documents/lifecycle/`](../backend/app/core/documents/lifecycle/) | 文档后台处理、发布、归档和重新索引 |
| [`core/retrieval/retriever.py`](../backend/app/core/retrieval/retriever.py) | 检索统一入口、发布状态过滤和 token 预算控制 |
| [`core/retrieval/hybrid_retriever.py`](../backend/app/core/retrieval/hybrid_retriever.py) | 向量检索与 BM25 的 RRF 融合 |
| [`core/retrieval/retrieval_orchestrator.py`](../backend/app/core/retrieval/retrieval_orchestrator.py) | 复杂问题拆解、多路召回和覆盖保护 |
| [`core/retrieval/indexing/`](../backend/app/core/retrieval/indexing/) | 向量化、关键词索引、主题和术语别名 |
| [`core/retrieval/planning/`](../backend/app/core/retrieval/planning/) | 查询改写和子问题规划 |
| [`core/retrieval/ranking/`](../backend/app/core/retrieval/ranking/) | 证据门槛与候选重排 |

### 业务服务、安全与存储

| 目录 | 职责 |
| --- | --- |
| [`services/jobs/`](../backend/app/services/jobs/) | 后台任务入队、执行、重试和恢复 |
| [`services/knowledge/`](../backend/app/services/knowledge/) | 文档治理状态和质量检查 |
| [`services/identity/`](../backend/app/services/identity/) | 角色申请业务 |
| [`services/operations/`](../backend/app/services/operations/) | 审计、评测和反馈业务 |
| [`auth/`](../backend/app/auth/) | 密码、JWT 和当前用户依赖 |
| [`authorization/`](../backend/app/authorization/) | 角色等级、资源权限和会话归属判断 |
| [`memory/`](../backend/app/memory/) | 短期会话记忆、长期用户记忆和缓存管理 |
| [`models/database.py`](../backend/app/models/database.py) | SQLAlchemy 数据库模型 |
| [`models/schemas.py`](../backend/app/models/schemas.py) | Pydantic API 请求与响应模型 |
| [`store/db.py`](../backend/app/store/db.py) | SQL 数据库连接、迁移、种子数据和依赖注入 |
| [`store/vector_store.py`](../backend/app/store/vector_store.py) | ChromaDB 文档及用户记忆向量封装 |
| [`llm/qwen.py`](../backend/app/llm/qwen.py) | 通义千问对话、流式生成、Embedding 和重排调用 |
| [`llm/prompt_templates.py`](../backend/app/llm/prompt_templates.py) | 回答、改写、规划、记忆和证据判断 Prompt |

## 前端入口

| 路径 | 职责 |
| --- | --- |
| [`frontend/src/main.ts`](../frontend/src/main.ts) | Vue、Pinia、路由和登录态初始化 |
| [`frontend/src/router/index.ts`](../frontend/src/router/index.ts) | 页面路由、懒加载与前端访问守卫 |
| [`frontend/src/views/`](../frontend/src/views/) | 按会话、身份、知识库和管理域组织的页面 |
| [`frontend/src/components/`](../frontend/src/components/) | 会话组件、文档上传组件和共享页头 |
| [`frontend/src/stores/session.ts`](../frontend/src/stores/session.ts) | 会话列表、消息和 SSE 流式状态 |
| [`frontend/src/stores/auth.ts`](../frontend/src/stores/auth.ts) | 登录用户及角色派生状态 |
| [`frontend/src/stores/knowledgeBases.ts`](../frontend/src/stores/knowledgeBases.ts) | 知识库列表和管理状态 |
| [`frontend/src/api/client.ts`](../frontend/src/api/client.ts) | Access Token、Cookie 和统一 `fetch` 封装 |
| [`frontend/src/api/conversation/chat.ts`](../frontend/src/api/conversation/chat.ts) | 聊天请求及 SSE 帧解析 |
| [`frontend/src/types.ts`](../frontend/src/types.ts) | 前端共享数据类型 |

## 核心调用链

### 用户发送一条消息

```text
ChatView.vue
  → stores/session.ts::sendMessage
  → api/conversation/chat.ts::chatStream
  → POST /api/chat
  → api/conversation/chat.py
  → core/rag_pipeline.py::answer_stream
  → core/retrieval/retriever.py
  → ChromaDB + BM25 + 证据门槛
  → llm/qwen.py::chat_completion_stream
  → SSE: meta → delta* → done | error
  → Pinia 增量更新页面
```

服务端不会信任客户端指定的知识库范围。聊天接口先根据当前用户计算可读知识库，再将这些知识库交给 RAG 流程检索。

### 上传并发布一份文档

```text
DocumentUpload.vue
  → api/knowledge/knowledgeBases.ts
  → api/knowledge/documents.py
  → 原文件落盘 + SQL 创建文档/版本
  → services/jobs/job_queue.py
  → core/documents/lifecycle/document_processing.py
  → 解析 → 清洗 → 切分 → 质量检查
  → pending_review / needs_change
  → api/knowledge/reviews.py
  → core/documents/lifecycle/document_publisher.py
  → 临时向量索引校验并切换正式索引
```

待审核文档不会写入正式检索索引；发布失败时旧版本继续提供服务。

### 用户与权限

```text
登录/刷新
  → auth/dependencies.py 解析 Bearer Token
  → authorization/policy.py 计算 guest/student/editor/admin 等级
  → 会话归属或知识库权限检查
  → 允许请求，或返回统一的 401/403 错误
```

前端路由守卫用于改善交互体验，后端权限策略才是安全边界。

## 按任务查代码

| 要做的事情 | 首先查看 |
| --- | --- |
| 修改聊天页面或流式显示 | `frontend/src/stores/session.ts`、`frontend/src/views/conversation/` |
| 修改 SSE 协议 | 前端 `api/conversation/chat.ts` 与后端 `api/conversation/chat.py` |
| 修改回答逻辑或 Prompt | `core/rag_pipeline.py`、`llm/prompt_templates.py` |
| 调整召回结果 | `core/retrieval/` 与 `config.py` 中的检索开关 |
| 支持新文档格式 | `core/documents/parsing/document_loader.py` |
| 修改切分规则 | `core/documents/parsing/chunker.py` |
| 修改审核发布流程 | `api/knowledge/reviews.py`、`core/documents/lifecycle/document_publisher.py` |
| 修改角色或知识库权限 | `authorization/policy.py` |
| 修改会话或长期记忆 | `memory/`、`api/conversation/memory.py` |
| 修改后台任务 | `services/jobs/`、`backend/worker.py` |
| 修改数据库结构 | `models/database.py`、`backend/alembic/versions/` |
| 增加前端页面 | `frontend/src/views/`、`frontend/src/router/index.ts` |
| 排查 API 错误 | `api/errors.py`、请求日志中的 `request_id` |

## 脚本入口

| 目录 | 用途 |
| --- | --- |
| [`backend/scripts/administration/`](../backend/scripts/administration/) | 本地管理员维护 |
| [`backend/scripts/evaluation/`](../backend/scripts/evaluation/) | 基线评测和招新数据集评测 |
| [`backend/scripts/knowledge/`](../backend/scripts/knowledge/) | 文档解析预览、索引演示和向量检查 |
| [`backend/scripts/smoke/`](../backend/scripts/smoke/) | DashScope 对话、Embedding 和流式冒烟验证 |

## 常用验证命令

```bash
# 后端测试
cd backend
../.venv/bin/python -m pytest -q

# 前端类型检查和生产构建
cd frontend
npm run build

# 无需 API Key 的文档解析预览
cd backend
../.venv/bin/python scripts/knowledge/parse_preview.py <文件路径>
```

## 推荐阅读顺序

1. 根目录 [`README.md`](../README.md)：启动项目并了解已有功能。
2. `frontend/src/stores/session.ts`：从用户发送消息的位置出发。
3. `backend/app/api/conversation/chat.py`：理解请求、会话和 SSE 契约。
4. `backend/app/core/rag_pipeline.py`：理解 RAG 主流程。
5. `backend/app/core/retrieval/`：深入召回、融合、证据判断和重排。
6. `backend/app/api/knowledge/` 与 `core/documents/`：理解知识如何进入正式索引。
7. `authorization/policy.py`、`memory/` 和 `models/database.py`：最后补齐权限、记忆和数据模型。
