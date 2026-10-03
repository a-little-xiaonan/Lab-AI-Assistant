# P1：公开 FAQ 模板与 Context Cache 接入方案

> 状态：讨论稿，依赖 P0 验证通过。
> 适用范围：公开、稳定的实验室招新 FAQ；不包含成员资料、动态日期、联系方式和任何非公开内容。

## 1. 目标

为少量高频招新问题建立人工审核的“证据模板”，将固定回答规则和固定证据卡置于 Prompt 前缀，将用户原始问题置于后缀。命中模板时可绕开一次常规检索、重排和大部分 Prefill；未命中或任何校验失败时，完整回退现有 RAG 链路。

第一批候选模板：

| code | 覆盖问题 |
| --- | --- |
| `recruitment_eligibility` | 零基础、非计算机专业是否可以报名 |
| `training_fee` | 培训或训练营是否收费 |
| `recruitment_process` | 报名、培训、考核、进入项目组的流程 |
| `lab_directions` | 实验室的主要研究或学习方向 |

模板是否进入首批名单必须以脱敏日志统计、人工复核和资料稳定性为准。所谓“20% 高频”还需证明这些问题会在缓存有效期内重复出现。

## 2. 数据模型

新增如下持久化模型，避免把可审计内容写死在代码中：

```text
FaqTemplate
  id, code, title
  kb_scope_json                 # 仅允许公开知识库集合
  audience_scope=public
  template_version
  prompt_policy_version
  knowledge_snapshot
  stable_prompt_hash
  evidence_bundle_json
  status=draft|active|stale|disabled
  created_at, updated_at

FaqTemplateEvidence
  template_id
  doc_id, document_version_id, chunk_index
  excerpt, citation_label, sort_order

FaqTemplateAlias
  template_id
  normalized_query, match_mode, priority
```

`FaqTemplateEvidence` 必须指向已发布文档版本的具体 Chunk。`excerpt` 是进入 Prompt 的受控摘录，而不是由请求时 LLM 临时改写的摘要。模板的 `knowledge_snapshot` 由所有关联文档的发布版本与内容哈希计算。

## 3. 请求路径

```text
POST /api/chat
  → 现有 list_readable_kbs() 计算用户可读范围
  → faq_router.match(query, readable_kb_ids)
      → 无候选：进入现有 intent_router / RAG
      → 有候选：template_validator.validate()
          → 失败：记录原因，进入现有 RAG
          → 成功：构造缓存 Prompt，调用 Context Cache adapter
              → 成功：使用绑定 Chunk 生成 sources，返回答案
              → 失败：记录原因，进入现有 RAG
```

FAQ 路由必须在鉴权之后执行。即便 P1 只支持公开资料，也不能信任客户端传入的知识库范围。

## 4. 路由设计

第一期不额外调用 LLM 路由，采用可测试的规则：

1. 规范化：去空白、统一中英文标点和大小写，处理已确认的口语别名；
2. 精确别名：`培训收钱吗`、`训练营要钱吗` 等映射到 `training_fee`；
3. 关键词组合：如“报名 + 流程”“培训 + 考核”映射到 `recruitment_process`；
4. 无法高置信度匹配时返回空，强制走常规 RAG。

路由器不得基于“实验室”“招新”等宽泛词直接命中，以免把资料不足的问题错误导入固定模板。

## 5. Prompt 契约

模板路径新增 `build_faq_answer_messages()`，不能修改通用 `build_rag_answer_messages()` 的消息顺序。

```text
System（稳定）
  固定回答规范、证据边界、拒答规则

稳定证据包（稳定，作为缓存边界）
  TEMPLATE=recruitment_process:v1
  SNAPSHOT=public-kb:<hash>
  [E1] 报名条件……
  [E2] 培训与考核……

动态后缀（不可缓存）
  当前用户问题：{query}
  必要的最近对话：{minimal_history}
```

回答仍须由模型基于证据包生成；`sources` 从绑定的 Evidence 行构造，保持当前 API 的来源字段契约。用户问题若要求模板未覆盖的细节，Prompt 应要求明确说明资料不足，而不是补写常识。

## 6. 代码与迁移范围

| 模块 | 预期职责 |
| --- | --- |
| `models/database.py` 与 Alembic | FAQ 三张表、索引和外键 |
| `core/faq/router.py` | 问题规范化、别名/关键词匹配 |
| `core/faq/service.py` | 模板读取、快照校验、证据构造 |
| `llm/prompt_templates.py` | FAQ 专用消息构造 |
| `llm/qwen.py` | 使用 P0 验证的缓存 adapter，返回 usage 指标 |
| `core/rag_pipeline.py` | 在常规检索前调用可降级 FAQ 分支 |
| `scripts/knowledge/` | 模板种子/校验脚本，仅管理员本地运行 |
| `tests/` | 命中、误路由、资料失效、模型异常回退测试 |

P1 不增加前端模板管理页。首批模板由受控种子脚本创建，证据与别名均经过人工审阅后设为 `active`。

## 7. 验收

- 所有模板引用的文档和 Chunk 均为当前已发布公开版本；
- 命中模板时，回答与 sources 符合现有聊天 API 契约；
- 未命中、资料失效、缓存调用异常时，均回退正常 RAG；
- 模板问答集的正确性、引用一致性与拒答准确性不低于常规 RAG 基线；
- P0 确认支持时，热请求能记录缓存命中和 TTFT 指标。

## 8. 风险与回退

- **模板覆盖过宽**：路由阈值不足即回退 RAG，模板不追求召回率最大化。
- **模板跳过检索造成资料遗漏**：模板仅覆盖边界明确的问题；评测集中添加“近似但不属于模板”的反例。
- **缓存无收益**：保留 FAQ 模板但关闭缓存开关，或整体关闭 FAQ 分支；不修改既有 RAG。
- **证据卡过长**：先按实际 Token 与命中窗口分析，避免为达到缓存门槛填充无关资料。
