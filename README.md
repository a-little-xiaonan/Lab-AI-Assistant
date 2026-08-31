# RAG AI Assistant

基于 RAG（检索增强生成）的智能助手：上传本地文档建立知识库，用自然语言提问，回答带引用来源。

- 后端：FastAPI + 通义千问（DashScope qwen-plus / text-embedding-v3）+ ChromaDB + MySQL/SQLite
- 前端：Vite + Vue 3 + TypeScript + Element Plus（聊天界面）
- 技术设计文档：`docs/RAG-AI-Assistant-技术设计文档.md`；分阶段开发文档：`docs/development/`
- 当前进度：**Phase 3 全部完成**（混合检索 / 查询改写 / 重排 / 重新索引 / 长期记忆 / 前端知识库管理界面），Phase 4 生产化按路线图推进

## 快速开始

```bash
# 1. 安装依赖（需 uv；已装则跳过）
#    brew install uv        # 或官方脚本：curl -LsSf https://astral.sh/uv/install.sh | sh

cd backend
# 2. 安装 spaCy 模型（unstructured 句子切分用；不在 PyPI，走 GitHub 镜像）
#    已装过可跳过，查看：uv pip show en-core-web-sm
uv pip install "https://gh-proxy.com/https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"

# 3. 安装项目依赖
uv pip install -r requirements.txt

# 4. 配置 API Key（阿里云百炼 https://bailian.console.aliyun.com 创建）
#    从项目根目录执行：
cd ..
cp .env.example .env
#    然后编辑 .env，填入 DASHSCOPE_API_KEY=sk-xxxx

# 5. 启动后端（MySQL 需先启动容器；默认端口 8100，因为本机 8000 被 Docker 里的 chromadb 容器占用）
docker start mysql
cd backend
../.venv/bin/uvicorn app.main:app --reload --port 8100
```

数据库自动建库建表（`rag_assistant`，utf8mb4），无需手动 CREATE DATABASE；启动时自动创建默认知识库 `kb_default` 并迁移旧数据。连接配置在 `.env` 的 `DATABASE_URL`；想切回 SQLite 改一行 `DATABASE_URL=sqlite:///./data/app.db` 即可。

启动后：接口文档 http://localhost:8100/docs ，健康检查 `GET /api/health`。

## 前端

```bash
cd frontend
npm install      # 走 npmmirror 镜像（.npmrc 已配置）
npm run dev      # http://localhost:5173（/api 代理到后端 8100）
npm run build    # 类型检查 + 生产构建
```

功能：知识库选择、会话管理（新建/切换/删除）、SSE 流式打字机输出（可停止）、来源折叠面板、Markdown + 代码高亮。

## 验证

```bash
# 无 key 也能玩的：解析预览（看任意文件切出多少块、元数据对不对）
../.venv/bin/python scripts/parse_preview.py ../docs/RAG-AI-Assistant-技术设计文档.md

# 有 key 后：冒烟测试（对话 + embedding 1024 维）
../.venv/bin/python scripts/smoke_test.py

# 流式实测（SSE 逐字输出）
../.venv/bin/python scripts/smoke_test_stream.py

# 评估基线（跑 docs/eval/qa-samples.md 样例集 → results-日期.md，人工三档打分）
../.venv/bin/python scripts/eval_run.py

# 入库演示（把 tests/fixtures 下样例文档入库并检索；上传已异步化，脚本内会轮询状态）
../.venv/bin/python scripts/index_demo.py

# 单元测试（不需要 key）
../.venv/bin/python -m pytest tests/ -v
```

## API 示例（curl）

```bash
# ---- 知识库 ----
curl -X POST http://localhost:8100/api/knowledge-bases -H "Content-Type: application/json" \
  -d '{"name": "产品手册", "description": "产品文档库"}'        # 创建（重名 409）
curl http://localhost:8100/api/knowledge-bases                  # 列表（含文档/chunk 统计）
curl http://localhost:8100/api/knowledge-bases/{id}             # 详情（含文档列表）
curl -X DELETE http://localhost:8100/api/knowledge-bases/{id}   # 删除（级联清向量/文件；kb_default 禁止）

# ---- 文档（Phase 2 起上传异步化：立即返回 202 + processing，轮询列表等 ready/failed）----
curl -X POST http://localhost:8100/api/knowledge-bases/kb_default/documents -F "file=@手册.pdf"
curl http://localhost:8100/api/knowledge-bases/kb_default/documents     # 轮询处理状态
curl -X DELETE http://localhost:8100/api/knowledge-bases/kb_default/documents/{doc_id}
curl http://localhost:8100/api/documents/{doc_id}/chunks                # chunk 明细（内容/大小/位置）

# ---- 问答 ----
# 非流式：回答末尾带「参考来源：」汇总（文件名 + 页码 + 片段摘要）
curl -X POST http://localhost:8100/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "千问怎么定价？", "knowledge_base_id": "kb_default"}'

# 流式（SSE：event: meta → delta* → done | error）
curl -N -X POST http://localhost:8100/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "千问怎么定价？", "stream": true}'

# ---- 会话管理（消息落库，短期记忆随会话恢复）----
curl -X POST http://localhost:8100/api/chat/sessions          # 创建
curl http://localhost:8100/api/chat/sessions                  # 列表
curl http://localhost:8100/api/chat/sessions/{id}             # 详情（含消息）
curl -X DELETE http://localhost:8100/api/chat/sessions/{id}   # 删除

# ---- 统计 ----
curl http://localhost:8100/api/stats
```

统一错误结构：`{"detail": {"code": "...", "message": "..."}}`（如 `api_key_missing`、`duplicate_document`、`knowledge_base_not_found`）。

### SSE 事件协议（前后端契约）

| 事件 | data | 说明 |
| ---- | ---- | ---- |
| `meta` | `{session_id}` | 首帧，告知会话 ID |
| `delta` | `{text}` | 增量文本，多条（含末尾汇总块） |
| `done` | `{full_text, sources}` | 终帧，完整回答 + 引用来源 |
| `error` | `{code, message}` | 异常时替代 done，随后正常关闭 |

## 目录结构

```
backend/app/
├── main.py              # FastAPI 入口（CORS、路由注册、统一异常处理）
├── config.py            # 配置单例（唯一读环境变量处，相对路径锚定项目根）
├── api/                 # 路由：health / stats / knowledge_base / documents / chat
├── core/                # document_loader / chunker / embedder / retriever / rag_pipeline
│   └── document_processing.py   # 上传后台处理 worker（解析→向量化→状态机）
├── llm/                 # qwen.py（对话+流式+向量化+重试）、prompt_templates.py、errors.py
├── store/               # vector_store.py（ChromaDB 薄封装）、db.py（SQLite/MySQL）
├── models/              # schemas.py（Pydantic）、database.py（SQLAlchemy 表）
└── memory/              # short_term.py（滑动窗口+摘要压缩）、memory_manager.py（实例注册表）
backend/scripts/         # smoke_test / smoke_test_stream / index_demo / parse_preview / eval_run
backend/tests/           # 单测（无需 API key）
frontend/                # Vite + Vue3 + TS + Element Plus（聊天界面）
docs/eval/               # QA 评估样例集 + 基线结果
data/                    # 运行时数据（gitignore）：chroma/、uploads/、app.db
```

## 已知事项

- **文档格式**：PDF（PyMuPDF）/ DOCX（python-docx）/ MD / TXT 专用解析；PPTX/HTML 走 unstructured；**Excel 用 openpyxl 直读行结构**（unstructured 会把整个 sheet 拍平成单块文本，产不出规格要求的行区块与 row_range 元数据）；未知格式走四级兜底链（L1 专用 → L2 unstructured → L3 文本试探 UTF-8/GBK → L4 优雅拒绝标记 failed）
- **分块**：结构分块（标题节/slide/页内段落等）+ 固定分块（512 字/块、64 字重叠、overlap 不跨结构块），中文句子优先不切断
- **spaCy 模型**：unstructured 首次解析 PPT/Excel 时会自动下载 en_core_web_sm（走 GitHub，国内可能失败），所以上面第 2 步预先装好
- **阈值 0.45**：检索相似度阈值（cosine 语义，similarity = 1 - distance），实测标定（2026-08-30）；**混合检索开启后不再硬过滤**（关键词独有命中不能被阈值误杀），仅作观测日志；关闭混合时恢复 Phase 2 行为
- **混合检索**（Phase 3-06）：向量 ∥ BM25（jieba 分词，进程内索引懒构建，与 ChunkRecord 表同步）→ RRF 融合 → top-20 候选；`HYBRID_RETRIEVAL_ENABLED=false` 完全回退 Phase 2
- **查询改写**（Phase 3-01）：LLM 扩展 2-3 条检索查询并发召回，只喂向量侧；失败自动降级原查询
- **重排**（Phase 3-02）：DashScope rerank API（`gte-rerank-v2`，实测可用；`gte-rerank` 需单独开通），默认关（评估后开）；失败降级 RRF 原顺序
- **重新索引**（Phase 3-05）：`POST /api/knowledge-bases/{id}/reindex`（单文档/全库）+ 双 buffer 切换（重建期间检索零中断）；重建中重复触发/上传 → 409；内容变更后重建即可生效
- **短期记忆**：滑动窗口 10 轮 + LLM 摘要压缩（超窗自动触发）；摘要是进程内运行时态，重启后从消息表重建窗口；不同会话记忆隔离
- **长期记忆**（Phase 3-03）：每轮对话后台提取（LLM → 置信度过滤 → 向量入库 `kb_{kb_id}_memory`），提问时召回相关记忆拼入 prompt（参考资料 → 相关记忆 → 对话历史 → 问题）；按知识库隔离、跨会话共享；清理入口 `DELETE /api/memory/{session_id}`
- **上传异步化**：Phase 2 起上传立即返回 202 + processing，后台处理完成后 status=ready/failed（失败带 error_message），前端轮询列表
- **引用规范**：正文 `[来源: 文件名 P页码]` + 末尾「参考来源：」汇总段（同源合并、snippet ≤50 字）；幻觉防护（越界引用剔除、直写不存在的来源剔除）
- **评估基线**：`docs/eval/qa-samples.md` 18 条样例 + `scripts/eval_run.py`，Phase 3 每个增强跑同一批对比
- **并发**：ChromaDB 写入进程内锁串行（单进程 uvicorn 前提，`--workers >1` 需升级锁方案）
- Phase 3：Query Rewrite / Re-ranking / 长期记忆 / 混合检索 / 文档重索引 / 前端知识库管理
