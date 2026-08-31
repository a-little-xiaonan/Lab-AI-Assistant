# Phase 2 · 01 流式输出 (SSE) — 开发文档

> **所属阶段**：Phase 2 — 核心功能完善
> **路线图条目**：§11 Phase 2 第 1 项「流式输出 (SSE)」
> **参考章节**：§4.2 RAG Pipeline（Generate 流式输出）· §8.1 聊天接口（stream 参数）
> **前置依赖**：Phase 1-07 聊天 API（非流式）
> **状态**：待开发

## 1. 目标与范围

`POST /api/chat` 支持 `stream=true`，以 SSE 逐 token 推送回答，前端可边收边显示（打字机效果）。非流式路径（`stream=false`）保持兼容。

## 2. 任务拆解

- [ ] `app/llm/qwen.py`：`chat_completion` 支持 `stream=True`，迭代返回增量文本（SDK 流式事件 → 字符串生成器）
- [ ] `app/core/rag_pipeline.py`：新增 `answer_stream(query, kb_id, session_id)` 生成器函数
  - 检索、拼装流程与非流式复用同一套代码
  - 生成阶段改为迭代 yield 增量文本
- [ ] `app/api/chat.py`：`stream=true` 时返回 `StreamingResponse(media_type="text/event-stream")`
- [ ] **SSE 事件协议**（前后端契约）：
  - `event: meta` → `{session_id}`（首帧，告知会话）
  - `event: delta` → `{text}`（增量片段，多条）
  - `event: done` → `{full_text, sources}`（终帧，一次性下发引用来源）
  - `event: error` → `{code, message}`（异常时发送，随后正常关闭连接）
- [ ] 中断处理：客户端断开（`request.is_disconnected()` 或生成器抛 `CancelledError`）→ 停止 LLM 流式调用，回收资源，不写半条消息到数据库
- [ ] 落库策略：回答完整后落库（或周期累积后落库），避免流式中途写脏数据
- [ ] 冒烟：`curl -N` 观察逐字输出与终帧结构

## 3. 设计要点

- **引用来源在 `done` 帧一次性下发**：检索结果在生成前已知，但跟随终帧下发最省前端逻辑（前端只处理两种消息：增量文本、终帧）
- **错误不走 HTTP 中断**：生成中途出错发 `error` 事件并正常关闭连接，前端统一按事件处理（SSE 的 HTTP 状态码不可靠）
- **会话持久化**：user 消息在请求开始时落库；assistant 回答在流结束后落库（内容完整）
- 与 Phase 1-07 的非流式路径共享 `rag_pipeline` 的检索与拼装代码，只替换生成环节

## 4. 涉及文件

```
backend/app/
├── llm/qwen.py          # 流式对话封装
├── core/rag_pipeline.py # answer_stream 生成器
├── api/chat.py          # SSE 路由分支（stream=true）
└── models/schemas.py    # SSE 事件数据结构（可选，用于文档化）
```

## 5. 验收标准

- [ ] `curl -N -X POST /api/chat`（stream=true）看到 `event: delta` 逐字输出，结尾收到 `event: done`（含完整回答与 sources）
- [ ] 流式回答完整落库（流结束后 `GET /api/chat/sessions/{id}` 能看到完整消息）
- [ ] 客户端中途断开 → 服务端日志记录中断，无残留半条消息、无连接泄漏
- [ ] `stream=false` 路径行为与 Phase 1-07 完全一致（回归）

## 6. 风险与注意事项

- **反向代理缓冲**（附录 B 相关）：生产环境（nginx 等）默认缓冲 SSE，会导致前端等到流结束才有内容；上线时需关闭 proxy buffering（Phase 4 处理，开发环境无此问题）
- **心跳保活**：网络代理长时间无数据会掐断连接；长回答若连续无输出，可定期发 `event: ping`（MVP 可后置）
- **超时**：LLM 流式响应较慢时注意读写超时配置要放宽（流式是持续小包，不是一次性响应）
