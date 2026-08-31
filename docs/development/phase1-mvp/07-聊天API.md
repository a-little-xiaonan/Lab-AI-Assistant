# Phase 1 · 07 简单聊天 API（非流式）— 开发文档

> **所属阶段**：Phase 1 — MVP
> **路线图条目**：§11 Phase 1 第 7 项「简单的聊天 API（非流式）」
> **参考章节**：§8.1 聊天接口 · §10 models/ 目录 · §12 DATABASE_URL
> **前置依赖**：Phase 1-06 基础 RAG Pipeline
> **状态**：待开发

## 1. 目标与范围

提供可被前端调用的聊天接口与会话管理（创建 / 列表 / 历史 / 删除），会话与消息落 SQLite 持久化，为 Phase 2-03 短期记忆提供数据源。**非流式**：一次请求返回完整回答（`stream` 参数先固定 `false`，字段预留）。

## 2. 任务拆解

- [ ] 安装：`sqlalchemy`（ORM）、`pydantic`（已有）
- [ ] `app/models/database.py`：SQLAlchemy 表定义
  - `Session`：id（`sess_` 前缀 UUID）、knowledge_base_id、created_at、updated_at
  - `Message`：id、session_id（外键）、role（user/assistant）、content、created_at
- [ ] `app/store/db.py`：engine / session 工厂，启动时建表（`create_all`）
- [ ] `app/models/schemas.py`：Pydantic 请求/响应模型（对齐 §8.1）：
  - `ChatRequest {session_id, knowledge_base_id, message, stream=false}`
  - `ChatResponse {answer, sources: [...]}`
  - `SessionOut {id, created_at, ...}`、`MessageOut {role, content, created_at}`
- [ ] `app/api/chat.py` 路由（§8.1）：
  - `POST /api/chat`：校验 → 调 `rag_pipeline.answer` → 存消息（user + assistant）→ 返回 JSON
  - `POST /api/chat/sessions`、`GET /api/chat/sessions`、`GET /api/chat/sessions/{id}`、`DELETE /api/chat/sessions/{id}`
- [ ] 全局异常处理：`LLMError` / 检索错误 / 参数错误 → 统一 JSON 错误结构 `{detail: {code, message}}`
- [ ] 冒烟：curl 完成 建会话 → 提问 → 查历史 全流程

## 3. 设计要点

- **session_id 由后端生成**：客户端不传则新建会话并返回 id；前端只需保存这个 id（对齐 §8.1）
- **消息落库**：user 消息与 assistant 回答都存（含回答耗时等可选字段），这是 Phase 2-03 短期记忆与 Phase 3-03 长期记忆提取的数据源
- **stream 字段预留**：接口签名保持 `{session_id, knowledge_base_id, message, stream}`，MVP 仅支持 `stream=false`；`stream=true` 返回 400 提示"Phase 2 支持"（或直接忽略，二选一，保持一致）
- **错误结构统一**：后续所有 API 复用 `{detail: {code, message}}`，前端按 code 做提示，不解析文案
- 知识库未指定时的行为：MVP 使用默认知识库 `kb_default`（Phase 1-04 约定），Phase 2-02 多知识库上线后改为必填

## 4. 涉及文件

```
backend/app/
├── models/
│   ├── schemas.py       # Pydantic 请求/响应
│   └── database.py      # SQLAlchemy: Session / Message 表
├── store/db.py          # engine、会话工厂、建表
├── api/chat.py          # 聊天 + 会话 CRUD 路由
├── main.py              # 注册路由 + 异常处理器
└── config.py            # 追加 DATABASE_URL 校验
```

## 5. 验收标准

- [ ] `curl -X POST /api/chat` 发送问题 → 返回 `{answer, sources}`，answer 与 Phase 1-06 直调一致
- [ ] 同一 session 连续提问两次，第二次的回答考虑到了第一次的上下文（历史已拼入 prompt）
- [ ] 会话 CRUD 四接口全部可用；`DELETE` 后历史清空
- [ ] 重启服务后 `GET /api/chat/sessions` 历史仍在（SQLite 持久化）
- [ ] 传入未知 session_id 时行为明确（报错或自动创建，文档记录并保持一致）

## 6. 风险与注意事项

- **无认证**：MVP 接口公开（任何本地请求可访问），属预期内，Phase 4 补认证与限流（附录 B）
- **SQLite 并发**：MVP 单进程够用；并发写注意 `check_same_thread=False` 与连接池配置
- **历史拼入 prompt 的 token 控制**：本轮先透传 `history`（Phase 1-06 预留），对话超过窗口上限的问题由 Phase 2-03 滑动窗口解决，不要在 API 层自作主张截断消息
- 回答的 `sources` 结构即前后端契约：`[{source_file, page, snippet}]`，与 Phase 2-05 前端渲染对齐
