# Phase 1 · 03 DashScope SDK 集成 — 开发文档

> **所属阶段**：Phase 1 — MVP
> **路线图条目**：§11 Phase 1 第 3 项「DashScope SDK 集成（千问对话 + Embedding）」
> **参考章节**：§3 技术栈（LLM / Embedding）· §12 环境变量 · 附录 B（API 限流/不稳定）
> **前置依赖**：Phase 1-02 FastAPI 框架搭建
> **状态**：待开发

## 1. 目标与范围

打通通义千问对话（`qwen-plus`）与 `text-embedding-v3` 向量化两条调用链路，封装为 `app/llm/` 层，供后续所有阶段复用。

**范围**：仅 SDK 封装与冒烟验证，不含业务编排。

## 2. 任务拆解

- [ ] 安装 DashScope SDK（`dashscope`，或使用 OpenAI 兼容模式客户端）
- [ ] `app/llm/qwen.py`：
  - `chat_completion(messages, **kwargs)`：非流式对话封装，`stream` 参数预留（Phase 2-01 实现流式）
  - `embed_texts(texts: list[str]) -> list[list[float]]`：批量向量化
- [ ] 重试与容错（对齐附录 B）：
  - 指数退避重试（默认 3 次，429/5xx 触发）
  - 超时设置（connect / read），超时抛自定义 `LLMError`
  - 备用模型切换预留：`LLM_MODEL_FALLBACK` 配置（qwen-plus → qwen-max）
- [ ] 速率控制：embedding 按批次调用（建议 16 条/批），批间间隔限制 QPS
- [ ] `scripts/smoke_test.py`：一条对话 + 一条 embedding 的冒烟脚本（不入测试套件，手动执行）

## 3. 设计要点

- **统一异常**：自定义 `LLMError`（含错误码与可读信息），上层 pipeline 捕获后转为友好错误响应，不让 SDK 原始异常泄漏到接口层
- **Embedding 维度断言**：`text-embedding-v3` 固定 1024 维，冒烟脚本断言 `len(vector) == 1024`；后续向量入库前同样校验（防止模型配置错误导致脏数据）
- **确定性**：相同文本的 embedding 结果应一致（无随机性），冒烟脚本可验证
- API Key 只从 `settings.DASHSCOPE_API_KEY` 读取，启动时校验非空且格式合法（`sk-` 前缀），缺失时日志给出明确指引

## 4. 涉及文件

```
backend/app/llm/
├── __init__.py        # 导出 llm 单例
├── qwen.py            # 对话 + embedding 封装、重试、限流
└── errors.py          # LLMError 定义（或并入 qwen.py）

backend/
├── config.py          # 追加 LLM_MODEL、EMBEDDING_MODEL、LLM_MODEL_FALLBACK、LLM_TIMEOUT 等字段
└── scripts/smoke_test.py   # 冒烟验证
```

## 5. 验收标准

- [ ] 冒烟脚本对话调用返回正常文本，且能识别 `qwen-plus`
- [ ] 冒烟脚本 embedding 返回 1024 维向量，同文本两次调用结果一致
- [ ] 配置无效 API Key 时，错误信息清晰可定位（含状态码、错误码）
- [ ] 手动模拟 429（临时用错误 key 或限流测试）能触发重试逻辑且最终报错可控

## 6. 风险与注意事项

- **Key 泄露**：`DASHSCOPE_API_KEY` 只存在于 `.env`（gitignore 覆盖），日志中禁止打印 key
- **限流（429）**：重试与退避是上线后的保命机制，MVP 就要有，不要后置（附录 B 第一条风险）
- **计费**：embedding 批量调用注意单批大小与 QPS 上限（DashScope 按 token 计费），批量凑满再调用可省费用
- **SDK 版本**：锁定 `dashscope` 版本号到 requirements.txt，避免上游 API 变更导致行为漂移
