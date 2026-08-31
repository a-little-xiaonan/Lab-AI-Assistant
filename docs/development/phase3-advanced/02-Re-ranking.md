# Phase 3 · 02 Re-ranking（重排序）— 开发文档

> **所属阶段**：Phase 3 — 高级功能
> **路线图条目**：§11 Phase 3 第 2 项「Re-ranking（重排序）」
> **参考章节**：§4.2 RAG Pipeline（Re-rank 环节）· 附录 B（检索质量风险）
> **前置依赖**：Phase 1-05（向量检索）、Phase 3-06（混合检索 + RRF 融合）、Phase 2-04（评估基线）
> **状态**：✅ 已完成（2026-08-31）

## 1. 目标与范围

对混合检索融合后的候选集（Phase 3-06）做二次精排，把真正相关的片段排到前面，提升上下文质量与回答准确率。实现两种后端，可配置切换。

## 2. 任务拆解

- [x] `app/core/reranker.py`：
  - `rerank(query, candidates: list[Chunk]) -> list[Chunk]`（返回重排后的有序列表）
  - **后端 A — 本地 Cross-Encoder**：如 `BAAI/bge-reranker-base`（transformers，需首次下载模型）
  - **后端 B — DashScope re-rank API**（实现时以官方文档为准，如无现成接口则后端 A 为准）
  - 由配置 `RERANKER_TYPE=local|api` 切换
- [x] `rag_pipeline` 接入：`混合检索 + RRF 融合（Phase 3-06）→ rerank → 取前 N 进上下文`
  - 重排在 **RRF 融合之后**执行（候选集 = 混合检索放宽后的 top-20），只对候选精排
  - `RERANK_TOP_N`（≤ 融合候选集规模，默认 5）决定最终进上下文的条数
- [x] 开关与参数：`RERANK_ENABLED`（默认关，评估后决定默认值）、`RERANK_TOP_N=5`、`RERANKER_TYPE`
- [x] 效果对比：样例集 重排开/关 对比，重点看**引用来源是否更准**（重排的价值在 precision，不在 recall）
- [x] 性能评估：记录单次重排耗时（本地模型首次加载时间 vs 每轮推理时间）

## 3. 设计要点

- **候选集规模**：重排只对混合检索融合后的候选集（top-20，见 Phase 3-06）做，不做全库精排 —— 控制延迟与成本
- **重排分数不替代阈值**：候选集已在 retriever 层过滤，重排只管排序，不再过滤（避免二次截断造成信息丢失）
- **本地模型资源**：首次加载需下载模型（几百 MB），加载到内存后每轮推理毫秒级；模型加载做懒加载 + 进程内复用
- **API 计费**：DashScope re-rank 按调用计费，评估时对比两者效果与成本再定默认后端
- 重排后顺序即 prompt 中参考资料顺序，靠前的片段模型关注度更高

## 4. 涉及文件

```
backend/app/core/
├── reranker.py         # 重排模块（新增，双后端）
└── rag_pipeline.py     # 检索后挂接重排

backend/app/config.py   # + RERANK_ENABLED / RERANK_TOP_N / RERANKER_TYPE
backend/requirements.txt # + transformers（本地模型后端时）
docs/eval/results-*.md  # 开/关对比记录
```

## 5. 验收标准

- [x] 构造含噪声候选的场景（top-k 中混入低相关片段）：重排后相关片段排在前 3
- [x] 开关切换无报错；`RERANK_ENABLED=false` 行为与 Phase 2 一致
- [x] 本地后端：首次加载可完成，后续推理 < 200ms/次（记录实测值）
- [x] 样例集对比记录归档：重排后引用来源准确率不劣于重排前
- [x] 重排失败（模型加载失败等）→ 降级为原顺序，回答正常

## 6. 风险与注意事项

- **模型体积与部署**：本地 Cross-Encoder 模型几百 MB，Docker 镜像会显著变大；若部署环境受限，用 API 后端
- **重排是锦上添花不是雪中送炭**：若召回本身很差（top-k 全是无关），重排救不回来 —— 此时应调 Query Rewrite / chunk 策略（附录 B 第三条风险的整体应对）
- 双后端并存会增加维护面：MVP 后可以收敛为效果与成本胜出的那一个
