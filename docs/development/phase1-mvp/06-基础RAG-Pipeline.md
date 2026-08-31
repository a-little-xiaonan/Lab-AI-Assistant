# Phase 1 · 06 基础 RAG Pipeline — 开发文档

> **所属阶段**：Phase 1 — MVP
> **路线图条目**：§11 Phase 1 第 6 项「基础 RAG Pipeline（检索 → 拼装 → 生成）」
> **参考章节**：§4.2 RAG 检索与生成 Pipeline · §6.1 上下文管理（Token 预算）
> **前置依赖**：Phase 1-05 ChromaDB 存储与检索
> **状态**：待开发

## 1. 目标与范围

实现「检索 → 拼装 → 生成」的完整链路：`rag_pipeline.answer(query, kb_id)` 输入问题、输出带引用来源的回答。不含流式（Phase 2-01）、不含 Query Rewrite / Re-rank / 记忆（Phase 3）。

## 2. 任务拆解

- [ ] `app/llm/prompt_templates.py`：按 §4.2 模板定义 `SYSTEM_PROMPT`
  - 仅基于参考资料回答、无信息要明确告知、中文回答、末尾标注引用
- [ ] `app/core/rag_pipeline.py`：核心编排函数 `answer(query, kb_id, history=[])`
  - 步骤 1 **检索**：`retriever.retrieve`（Phase 1-05，含阈值过滤与 token 截断）
  - 步骤 2 **拼装**：System Prompt + 参考资料 + 对话历史 + 用户问题（对齐 §4.2 / §7.4 的 prompt 结构；历史参数 MVP 先透传，Phase 2-03 接入真实记忆）
  - 步骤 3 **生成**：`qwen.chat_completion`（非流式）
  - 步骤 4 **后处理**：把 chunk 元数据（`source_file`、`page`）整理为引用标注，附在回答末尾
- [ ] 无检索结果分支：走「知识库中未找到相关内容」提示模板（细化在 Phase 2-04，MVP 先用简单版）
- [ ] `tests/test_rag_pipeline.py`：mock LLM 与检索的链路测试（假检索结果 → 断言 prompt 拼装与引用格式）

## 3. 设计要点

- **编排点**：`rag_pipeline` 是后续所有增强（Query Rewrite、Re-rank、长期记忆）的挂接点，函数签名保持稳定：`answer(query, kb_id, session_id=None, **flags)`
- **token 预算**（§6.1）：检索结果段 ≤ 3000 token（retriever 已截断）；历史段 ≤ 4000（Phase 2-03 管）；总预算在拼装时做最后一道检查，超限截断检索段
- **引用来源**：来自 chunk 元数据，回答末尾输出 `[来源: 产品手册.pdf P12]`；多来源按顺序列出
- **幻觉控制**：prompt 明确「仅基于参考资料回答」+ 引用标注双保险（附录 B 第三条风险的缓解之一）
- 异常路径：检索失败 → 降级为纯 LLM 回答并在日志标记；LLM 调用失败 → 抛 `LLMError` 由 API 层转为友好错误（不静默返回空）

## 4. 涉及文件

```
backend/app/core/rag_pipeline.py    # 核心编排（新增）
backend/app/llm/prompt_templates.py # 模板（新增）
backend/tests/test_rag_pipeline.py  # 链路测试（mock）
```

## 5. 验收标准

- [ ] 对已入库知识库提问 → 返回回答，末尾引用来源与检索结果一致（文件名、页码正确）
- [ ] 问与知识库无关的问题 → 回答明确告知未找到相关内容（不编造）
- [ ] 检索结果超过 3000 token 时被截断，回答仍正常生成
- [ ] mock 测试通过：LLM 收到的 prompt 包含 system / 参考资料 / 问题三部分
- [ ] 检索服务临时不可用时 → 链路降级或报错可控，不崩溃

## 6. 风险与注意事项

- **回答偏离检索内容（幻觉）**：MVP 靠 prompt 约束 + 引用标注缓解；质量量化评估在 Phase 2-04 建样例集，Phase 3 用 Query Rewrite / Re-rank 提升召回
- **prompt 拼装顺序**：参考资料 → 历史 → 问题 的顺序不要随意调整，模型对「最靠近问题的是当前问题」有依赖（§6.1 上下文窗口示意）
- 引用标注的格式即协议：Phase 2-04 会细化规范，后端与前端（Phase 2-05）要按同一格式解析
