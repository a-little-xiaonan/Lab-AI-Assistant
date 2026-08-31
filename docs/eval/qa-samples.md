# QA 评估样例集（Phase 2-04 基线）

用于 `backend/scripts/eval_run.py` 批量评估 RAG 效果，作为 Phase 3（Query Rewrite / Re-ranking 等）的对比基线。

- **类别约定**：`normal` 正常问答（知识库有答案）/ `no-answer` 无答案题（应明确告知未找到）/ `multi-source` 多来源题 / `followup` 追问题（依赖对话历史）
- **打分规则**（人工三档，1-3 分，填在 results 文件的空表里）：
  - 引用正确性：标注的来源是否真实存在于知识库且与回答内容对应
  - 回答准确性：内容是否与参考资料一致、无编造
  - 告知明确性：无答案时是否明确告知未找到并给出建议
- **对比原则**：Phase 3 每个增强跑同一批样例，对比只看相对变化；尽量同一人同批打分

```json
[
  {"id": 1, "category": "normal", "question": "qwen-plus 的输入价格是多少？"},
  {"id": 2, "category": "normal", "question": "qwen-plus 的计费方式是什么？"},
  {"id": 3, "category": "normal", "question": "qwen-max 和 qwen-plus 有什么区别？"},
  {"id": 4, "category": "normal", "question": "千问大模型上下文长度最长支持多少？"},
  {"id": 5, "category": "normal", "question": "API Key 应该通过什么环境变量传入？"},
  {"id": 6, "category": "normal", "question": "DashScope 平台上的对话模型都有哪些？"},
  {"id": 7, "category": "multi-source", "question": "不同模型的输入价格分别是多少？"},
  {"id": 8, "category": "multi-source", "question": "调用 API 需要什么前提条件？"},
  {"id": 9, "category": "no-answer", "question": "今天北京天气怎么样？"},
  {"id": 10, "category": "no-answer", "question": "这个系统支持接入 GPT-4o 吗？"},
  {"id": 11, "category": "no-answer", "question": "数据中心的服务器配置是什么？"},
  {"id": 12, "category": "no-answer", "question": "你们公司的团队规模有多大？"},
  {"id": 13, "category": "followup", "question": "那输出价格呢？", "note": "前置：先问 qwen-plus 定价"},
  {"id": 14, "category": "followup", "question": "那最长上下文是多少？", "note": "前置：先问千问的上下文长度"},
  {"id": 15, "category": "followup", "question": "我刚才问的第一个问题是什么？", "note": "依赖短期记忆"},
  {"id": 16, "category": "normal", "question": "如何开始使用 DashScope？"},
  {"id": 17, "category": "normal", "question": "不支持哪些格式的文档？"},
  {"id": 18, "category": "normal", "question": "这个助手支持哪些文档格式？"}
]
```
