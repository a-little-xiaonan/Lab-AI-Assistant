# 开发文档索引

本目录下每份开发文档对应《RAG-AI-Assistant-技术设计文档》§11 开发路线图中的**一个路线图条目**，按阶段组织。

> Phase 4（生产化）按开发意图暂不具体实现，仅提供一份简述性概述文档（`phase4-production/`）。

| 阶段 | 文档 | 对应路线图条目 |
| ---- | ---- | -------------- |
| Phase 1 — MVP | [01-项目初始化](phase1-mvp/01-项目初始化.md) | 项目初始化 |
| | [02-FastAPI框架搭建](phase1-mvp/02-FastAPI框架搭建.md) | 后端 FastAPI 框架搭建 |
| | [03-DashScope-SDK集成](phase1-mvp/03-DashScope-SDK集成.md) | DashScope SDK 集成（千问对话 + Embedding） |
| | [04-文档处理Pipeline](phase1-mvp/04-文档处理Pipeline.md) | 基础文档上传、解析、分块、向量化 |
| | [05-ChromaDB存储与检索](phase1-mvp/05-ChromaDB存储与检索.md) | ChromaDB 存储与检索 |
| | [06-基础RAG-Pipeline](phase1-mvp/06-基础RAG-Pipeline.md) | 基础 RAG Pipeline（检索 → 拼装 → 生成） |
| | [07-聊天API](phase1-mvp/07-聊天API.md) | 简单的聊天 API（非流式） |
| Phase 2 — 核心功能完善 | [01-流式输出SSE](phase2-core/01-流式输出SSE.md) | 流式输出 (SSE) |
| | [02-多知识库管理CRUD](phase2-core/02-多知识库管理CRUD.md) | 多知识库管理 CRUD |
| | [03-短期记忆](phase2-core/03-短期记忆.md) | 短期记忆（对话历史 + 滑动窗口） |
| | [04-Prompt优化与引用标注](phase2-core/04-Prompt优化与引用标注.md) | Prompt 优化与引用来源标注 |
| | [05-前端聊天界面](phase2-core/05-前端聊天界面.md) | 前端聊天界面 |
| Phase 3 — 高级功能 | [01-Query-Rewrite](phase3-advanced/01-Query-Rewrite.md) | Query Rewrite（查询改写） |
| | [02-Re-ranking](phase3-advanced/02-Re-ranking.md) | Re-ranking（重排序） |
| | [03-长期记忆系统](phase3-advanced/03-长期记忆系统.md) | 长期记忆系统 |
| | [04-前端知识库管理界面](phase3-advanced/04-前端知识库管理界面.md) | 前端知识库管理界面 |
| | [05-文档重新索引](phase3-advanced/05-文档重新索引.md) | 文档重新索引 |
| | [06-混合检索](phase3-advanced/06-混合检索.md) | 混合检索（关键词 + 向量，RRF 融合）|
| Phase 4 — 生产化 | [01-生产化概述（简述）](phase4-production/01-生产化概述.md) | 全部 5 项（暂不实现） |

---

**文档通用结构**：目标与范围 → 任务拆解 → 设计要点 → 涉及文件 → 验收标准 → 风险与注意事项。

**约定**：

- 各文档中的「参考章节」均指《RAG-AI-Assistant-技术设计文档》的章节号
- 目录结构、接口命名以设计文档 §10 为准，一旦定下不做破坏性变更
- 每个 Phase 的文档按顺序实施，后一文档的前置依赖为前一文档的验收项
