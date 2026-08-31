# RAG AI Assistant

基于 RAG（检索增强生成）的智能助手：上传本地文档建立知识库，用自然语言提问，回答带引用来源。

- 后端：FastAPI + 通义千问（DashScope qwen-plus / text-embedding-v3）+ ChromaDB + SQLite
- 技术设计文档：`docs/RAG-AI-Assistant-技术设计文档.md`；分阶段开发文档：`docs/development/`
- 当前进度：**Phase 1 MVP 后端已实现**（解析分块 → 向量化入库 → 检索 → 问答），前端与高级功能按路线图推进

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

# 5. 启动（MySQL 需先启动容器；默认端口 8100，因为本机 8000 被 Docker 里的 chromadb 容器占用）
docker start mysql
cd backend
../.venv/bin/uvicorn app.main:app --reload --port 8100
```

数据库自动建库建表（`rag_assistant`，utf8mb4），无需手动 CREATE DATABASE。连接配置在 `.env` 的 `DATABASE_URL`；想切回 SQLite 改一行 `DATABASE_URL=sqlite:///./data/app.db` 即可。

启动后：接口文档 http://localhost:8000/docs ，健康检查 `GET /api/health`。

## 验证

```bash
# 无 key 也能玩的：解析预览（看任意文件切出多少块、元数据对不对）
../.venv/bin/python scripts/parse_preview.py ../docs/RAG-AI-Assistant-技术设计文档.md

# 有 key 后：冒烟测试（对话 + embedding 1024 维）
../.venv/bin/python scripts/smoke_test.py

# 入库演示（把 tests/fixtures 下样例文档入库并检索）
../.venv/bin/python scripts/index_demo.py

# 单元测试（不需要 key）
../.venv/bin/python -m pytest tests/ -v
```

## API 示例（curl）

```bash
# 上传文档 → 解析分块 → 向量化入库（重复上传同内容返回 409）
curl -X POST http://localhost:8000/api/documents -F "file=@手册.pdf"

# 查看已上传文档
curl http://localhost:8000/api/documents

# 问答（非流式；回答末尾带 [来源: 文件名 P页码]）
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "千问怎么定价？", "knowledge_base_id": "kb_default"}'

# 会话管理
curl -X POST http://localhost:8000/api/chat/sessions          # 创建
curl http://localhost:8000/api/chat/sessions                  # 列表
curl http://localhost:8000/api/chat/sessions/{id}             # 详情（含消息）
curl -X DELETE http://localhost:8000/api/chat/sessions/{id}   # 删除
```

统一错误结构：`{"detail": {"code": "...", "message": "..."}}`（如 `api_key_missing`、`unsupported_format`、`duplicate_document`）。

## 目录结构

```
backend/app/
├── main.py              # FastAPI 入口（CORS、路由注册、统一异常处理）
├── config.py            # 配置单例（唯一读环境变量处，相对路径锚定项目根）
├── api/                 # 路由：health / stats / documents / chat / knowledge_base
├── core/                # 核心逻辑：document_loader / chunker / cleaner / embedder / retriever / rag_pipeline
├── llm/                 # qwen.py（对话+向量化+重试）、prompt_templates.py、errors.py
├── store/               # vector_store.py（ChromaDB 薄封装）、db.py（SQLite）
├── models/              # schemas.py（Pydantic）、database.py（SQLAlchemy 表）
└── memory/              # 记忆系统（Phase 2/3）
backend/scripts/         # smoke_test / index_demo / parse_preview
backend/tests/           # 25 个单测（无需 API key）
data/                    # 运行时数据（gitignore）：chroma/、uploads/、app.db
```

## 已知事项

- **文档格式**：PDF（PyMuPDF）/ DOCX（python-docx）/ MD / TXT 专用解析；PPTX/HTML 走 unstructured；**Excel 用 openpyxl 直读行结构**（unstructured 会把整个 sheet 拍平成单块文本，产不出规格要求的行区块与 row_range 元数据）；未知格式走四级兜底链（L1 专用 → L2 unstructured → L3 文本试探 UTF-8/GBK → L4 优雅拒绝标记 failed）
- **分块**：结构分块（标题节/slide/页内段落等）+ 固定分块（512 字/块、64 字重叠、overlap 不跨结构块），中文句子优先不切断
- **spaCy 模型**：unstructured 首次解析 PPT/Excel 时会自动下载 en_core_web_sm（走 GitHub，国内可能失败），所以上面第 2 步预先装好
- **阈值 0.3**：检索相似度阈值（cosine 语义，similarity = 1 - distance），MVP 建议值，按实际检索质量调整
- 单知识库 `kb_default`；多知识库、SSE 流式、短期记忆、前端在 Phase 2；混合检索/重排/长期记忆在 Phase 3
