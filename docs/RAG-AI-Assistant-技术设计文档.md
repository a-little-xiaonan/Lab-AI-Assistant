# RAG AI 助手 — 技术设计文档

> 版本：v0.1  
> 日期：2026-08-27  
> 状态：草案

---

## 1. 项目概述

构建一个基于 RAG（Retrieval-Augmented Generation）的智能助手，用户可以：

- 上传本地文档（PDF、Markdown、TXT、Word）建立知识库
- 通过自然语言提问，助手基于知识库内容生成回答
- 管理知识库（增删改查文档、查看索引状态）
- 维护多轮对话上下文与长期记忆

---

## 2. 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                          前端 (Vue 3)                           │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │   聊天界面    │  │  知识库管理   │  │  系统设置 / 监控      │  │
│  └──────┬───────┘  └──────┬───────┘  └──────────┬───────────┘  │
│         │                 │                      │              │
└─────────┼─────────────────┼──────────────────────┼──────────────┘
          │ HTTP/WS         │ HTTP                 │ HTTP
          ▼                 ▼                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                     后端 (FastAPI)                               │
│  ┌────────────┐  ┌─────────────┐  ┌──────────────────────────┐ │
│  │ Chat API   │  │ Document API│  │  Knowledge Base API      │ │
│  │ /chat      │  │ /documents  │  │  /knowledge-bases        │ │
│  └─────┬──────┘  └──────┬──────┘  └────────────┬─────────────┘ │
│        │                │                       │               │
│  ┌─────▼────────────────▼───────────────────────▼─────────────┐ │
│  │                   RAG Pipeline                              │ │
│  │  Query Rewrite → Retrieve → Re-rank → Generate             │ │
│  └─────┬──────────────────────────────────────┬───────────────┘ │
│        │                                      │                 │
│  ┌─────▼──────┐  ┌──────────┐  ┌─────────────▼───────────────┐ │
│  │ Embedding  │  │  LLM     │  │  Memory Manager             │ │
│  │ Service    │  │  Service │  │  (短期 + 长期记忆)            │ │
│  │ (DashScope)│  │ (千问)   │  │                              │ │
│  └─────┬──────┘  └──────────┘  └──────────────────────────────┘ │
│        │                                                         │
│  ┌─────▼──────────────────────────────────────────────────────┐ │
│  │              Vector Store (ChromaDB)                        │ │
│  │  ┌──────────┐  ┌──────────────┐  ┌───────────────────────┐ │ │
│  │  │ 文档向量  │  │  对话记忆向量 │  │  元数据索引           │ │ │
│  │  └──────────┘  └──────────────┘  └───────────────────────┘ │ │
│  └────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────┐
│   SQLite / 文件系统   │  ← 用户数据、文档原文、配置
└──────────────────────┘
```

---

## 3. 技术栈

| 层面           | 技术选型                            | 说明                              |
| -------------- | ----------------------------------- | --------------------------------- |
| **后端框架**   | FastAPI                             | 异步、自带 OpenAPI 文档           |
| **前端框架**   | Vue 3 + Element Plus                | 管理后台风格 UI，快速开发          |
| **LLM**        | 通义千问 (DashScope)                | qwen-plus / qwen-max 可选        |
| **Embedding**  | text-embedding-v3 (DashScope)       | 1024 维，中文优化                 |
| **向量数据库** | ChromaDB                            | 零配置、本地文件持久化             |
| **文档解析**   | Unstructured / PyMuPDF / python-docx| 多格式支持（PDF/DOCX/MD/TXT 专用解析，PPT/Excel/HTML 与未知格式走 unstructured 兜底链）|
| **数据库**     | SQLite                              | 存储用户、会话、文档元数据         |
| **包管理**     | uv / pip                            | Python 依赖管理                   |
| **容器化**     | Docker + Docker Compose             | 可选，便于部署                    |

---

## 4. 核心模块设计

### 4.1 文档处理 Pipeline

```
原始文档
  │
  ▼
┌─────────────┐
│ 文档解析     │  PDF → PyMuPDF, MD/TXT → 直读, DOCX → python-docx,
│ (Loader)     │  PPT/Excel/HTML → unstructured, 未知格式 → 四级兜底链
│              │  输出：元素列表 + 元数据（页码、slide、sheet）
└──────┬──────┘
       ▼
┌─────────────┐
│ 文本清洗     │  去除乱码、合并断行、去除页眉页脚
│ (Cleaner)    │
└──────┬──────┘
       ▼
┌─────────────┐
│ 文本分块     │  递归字符分割 / 语义分割
│ (Chunker)    │  chunk_size=512, overlap=64
└──────┬──────┘     保留 chunk 来源元数据
       ▼
┌─────────────┐
│ 向量化       │  DashScope text-embedding-v3
│ (Embedder)   │  批量异步调用，速率控制
└──────┬──────┘
       ▼
┌─────────────┐
│ 存入向量库   │  ChromaDB collection，附带元数据
│ (Store)      │  collection = knowledge_base_id
└─────────────┘
```

**分块策略细节：**

| 策略           | 适用场景               | 参数建议                    |
| -------------- | ---------------------- | --------------------------- |
| 递归字符分割   | 通用文档               | chunk=512, overlap=64       |
| 按段落/标题    | 结构化 Markdown        | 按 `#` / `\n\n` 切分        |
| 按 slide 切分  | PPT                   | 一个 slide 为一块            |
| 按 sheet/行区块| Excel                 | 每 ~50 行一块，表头重复      |
| 语义分割       | 高质量需求             | 使用 Embedding 相似度断句   |

> 未知/其他格式经四级兜底链处理：专用解析器 → unstructured → 按纯文本读 → 标记 failed 并提示转格式（详见 Phase 1-04 开发文档 §3.2）。

每个 chunk 存储的元数据：
```json
{
  "doc_id": "doc_abc123",
  "kb_id": "kb_xyz",
  "source_file": "产品手册.pdf",
  "page": 12,               // PDF/DOCX 有页概念；无则省略
  "slide_number": 3,        // PPT 文档
  "sheet_name": "Sheet1",   // Excel 文档
  "row_range": "1-50",      // Excel 行区块
  "chunk_index": 3,
  "created_at": "2026-08-27T10:00:00Z"
}
```

字段按格式**按需出现**：没有页/slide/sheet 概念的格式（如未知文本格式）只保留 `source_file` 等通用字段。

### 4.2 RAG 检索与生成 Pipeline

```
用户提问
  │
  ▼
┌──────────────┐
│ Query Rewrite│  可选：LLM 改写查询、扩展同义词、生成子问题
└──────┬───────┘
       ▼
┌──────────────────┐
│ 混合检索          │  向量检索（ChromaDB similarity_search）
│ (Hybrid Retrieve) │  ∥ 关键词检索（jieba 分词 + BM25）
│                   │  RRF 融合，Top-K = 5~10（融合候选放宽）
└──────┬───────────┘
       ▼
┌──────────────┐
│ 重排序        │  可选：Cross-Encoder re-rank
│ (Re-rank)     │  或 DashScope re-rank API
└──────┬───────┘
       ▼
┌──────────────┐
│ 上下文拼装    │  System Prompt + 检索结果 + 对话历史 + 用户问题
│ (Compose)     │  控制总 Token 在模型窗口内
└──────┬───────┘
       ▼
┌──────────────┐
│ LLM 生成      │  通义千问 API 调用
│ (Generate)    │  流式输出 (SSE)
└──────┬───────┘
       ▼
┌──────────────┐
│ 后处理        │  引用标注、来源追踪
│ (Post-process)│
└──────────────┘
```

**Prompt 模板示例：**
```
你是一个专业的知识助手。请仅基于以下参考资料回答用户问题。
如果参考资料中没有相关信息，请明确告知用户你无法找到相关内容。

## 参考资料
{retrieved_chunks_with_sources}

## 对话历史
{conversation_history}

## 用户问题
{user_query}

请用中文回答，并在回答末尾标注引用来源。
```

### 4.3 知识库管理

支持以下操作：

| 操作     | 说明                                      |
| -------- | ----------------------------------------- |
| 创建知识库 | 指定名称、描述、Embedding 模型            |
| 上传文档  | 文件上传 → 解析 → 分块 → 向量化 → 入库    |
| 删除文档  | 删除该文档对应的所有 chunk 向量            |
| 查看文档列表 | 展示已入库的文档及其 chunk 数量          |
| 重新索引  | 清除旧向量，重新处理文档                   |
| 知识库统计 | 文档数、chunk 数、存储大小                 |

---

## 5. 向量数据库设计

使用 ChromaDB，核心设计：

```
ChromaDB (persist_directory=./data/chroma)
  │
  ├── Collection: kb_{knowledge_base_id}_docs
  │     ├── documents: [chunk_text, ...]
  │     ├── embeddings: [vector_1024d, ...]
  │     ├── metadatas: [{doc_id, source, page, ...}, ...]
  │     └── ids: ["chunk_uuid_1", "chunk_uuid_2", ...]
  │
  └── Collection: kb_{knowledge_base_id}_memory
        ├── documents: [memory_text, ...]
        ├── embeddings: [vector_1024d, ...]
        ├── metadatas: [{session_id, type, timestamp, ...}, ...]
        └── ids: ["mem_uuid_1", ...]
```

**检索参数建议：**

| 参数               | 默认值  | 说明                        |
| ------------------ | ------- | --------------------------- |
| n_results (Top-K)  | 5       | 返回最相似的 K 个 chunk     |
| similarity_threshold | 0.3   | 低于阈值的结果过滤掉；混合检索模式下不作硬门槛（见下）|
| max_context_tokens | 3000    | 检索结果总 Token 上限       |
| 融合候选集（混合检索）| 20    | RRF 融合后放宽，供 Re-rank 精排 |

**混合检索（关键词 + 向量，Phase 3-06）**：向量侧（ChromaDB）与关键词侧（jieba 分词 + BM25 内存索引，与 ChromaDB 同源 chunk 文本）并行检索，RRF 融合（`score = Σ 1/(60+rank)`）去重后进 Re-rank。混合模式下 `similarity_threshold` 不作硬门槛（关键词独有的命中向量分可能低于阈值），改由 RRF 排序 + Re-rank 负责质量，0.3 保留为观测指标。

---

## 6. 上下文管理

### 6.1 对话上下文

```
┌─────────────────────────────────────────────────┐
│  对话上下文窗口                                    │
│                                                   │
│  [System Prompt]                                  │
│  [检索到的相关文档片段]                             │
│  [历史消息]  ←── 滑动窗口 or 摘要压缩              │
│  [当前用户问题]                                    │
│                                                   │
│  总 Token 预算 ≤ 模型上下文窗口 (如 32K)           │
└─────────────────────────────────────────────────┘
```

**上下文管理策略：**

| 策略         | 实现方式                              | 适用场景     |
| ------------ | ------------------------------------- | ------------ |
| 滑动窗口     | 保留最近 N 轮（如 10 轮）对话         | 短对话为主   |
| 摘要压缩     | 超出窗口时，LLM 摘要旧对话后替换      | 长对话       |
| Token 预算   | 动态计算已用 Token，优先保留近期对话   | 精确控制     |

**Token 分配预算：**
```
模型窗口 (32K) = System Prompt (500)
               + 检索上下文 (3000)
               + 对话历史 (4000)
               + 用户问题 (500)
               + 模型回答预留 (4000)
               + 缓冲 (20000)
```

### 6.2 Query Rewrite（查询改写）

可选但推荐，提升检索质量：

```python
# 多查询策略：将用户问题扩展为多个检索查询
original_query = "千问的定价是多少？"
# → 改写为：
# 1. "通义千问 API 价格"
# 2. "千问模型收费标准"  
# 3. "DashScope 计费方式"
```

---

## 7. 记忆系统设计

### 7.1 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│                       Memory Manager                         │
│                                                              │
│  ┌─────────────────┐    ┌─────────────────────────────────┐ │
│  │   短期记忆       │    │   长期记忆                       │ │
│  │   (In-Memory)    │    │   (Persistent)                  │ │
│  │                  │    │                                 │ │
│  │  当前会话的      │    │  用户偏好、高频问答、             │ │
│  │  完整消息列表    │    │  提取的关键实体和事实             │ │
│  │                  │    │                                 │ │
│  │  存储：内存      │    │  存储：ChromaDB memory collection│ │
│  │  生命周期：会话   │    │  生命周期：永久                   │ │
│  └────────┬────────┘    └──────────────┬──────────────────┘ │
│           │                            │                     │
│           ▼                            ▼                     │
│  ┌─────────────────┐    ┌─────────────────────────────────┐ │
│  │  摘要压缩器      │    │  记忆提取器                       │ │
│  │                  │    │                                 │ │
│  │  当对话超出窗口  │    │  每轮对话后，LLM 提取：           │ │
│  │  时，生成摘要    │    │  - 用户偏好                      │ │
│  │  替换旧消息      │    │  - 关键事实                      │ │
│  │                  │    │  - 待跟进事项                    │ │
│  └─────────────────┘    └─────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

### 7.2 短期记忆

```python
class ShortTermMemory:
    """会话级别的对话历史"""
    
    def __init__(self, session_id: str, max_turns: int = 10):
        self.session_id = session_id
        self.messages: list[Message] = []  # 完整消息列表
        self.summary: str | None = None    # 被压缩的旧对话摘要
        self.max_turns = max_turns
    
    def add_message(self, role: str, content: str):
        self.messages.append(Message(role=role, content=content))
        if len(self.messages) > self.max_turns * 2:
            self._compress()
    
    def _compress(self):
        """将旧对话压缩为摘要"""
        old_messages = self.messages[:self.max_turns]
        self.summary = llm_summarize(old_messages)
        self.messages = self.messages[self.max_turns:]
    
    def get_context(self) -> str:
        """获取用于拼装 Prompt 的上下文"""
        context = ""
        if self.summary:
            context += f"[历史摘要] {self.summary}\n\n"
        for msg in self.messages:
            context += f"{msg.role}: {msg.content}\n"
        return context
```

### 7.3 长期记忆

```python
class LongTermMemory:
    """跨会话持久化记忆，存入向量库"""
    
    # 记忆类型
    MEMORY_TYPES = {
        "user_preference": "用户偏好（语言风格、领域偏好等）",
        "key_fact": "关键事实（用户提到的重要信息）",
        "faq_pair": "高频问答对（常见问题的标准回答）",
        "entity": "实体关系（人名、产品名、概念关联）",
    }
    
    def extract_and_store(self, session_id: str, messages: list[Message]):
        """每轮对话后提取值得记忆的信息"""
        prompt = f"""
        分析以下对话，提取值得长期记忆的信息。
        仅提取非显而易见的、有长期价值的信息。
        
        对话内容：{format_messages(messages)}
        
        返回 JSON 数组，每项包含：
        - type: {list(self.MEMORY_TYPES.keys())}
        - content: 记忆内容
        - confidence: 置信度 (0-1)
        """
        memories = llm_extract(prompt)
        for mem in memories:
            if mem["confidence"] > 0.7:
                self.vector_store.add(
                    collection="memory",
                    text=mem["content"],
                    metadata={"type": mem["type"], "session_id": session_id}
                )
    
    def recall(self, query: str, session_id: str, top_k: int = 3) -> list[str]:
        """召回与当前问题相关的长期记忆"""
        return self.vector_store.query(
            collection="memory",
            query_text=query,
            where={"session_id": session_id},
            n_results=top_k
        )
```

### 7.4 完整的上下文组装

```python
def build_prompt(query: str, session_id: str, kb_id: str) -> str:
    """组装最终发给 LLM 的完整 Prompt"""
    
    # 1. 检索知识库文档
    docs = vector_store.query(collection=f"kb_{kb_id}_docs", query_text=query)
    
    # 2. 召回长期记忆
    memories = long_term_memory.recall(query, session_id)
    
    # 3. 获取短期对话历史
    history = short_term_memory.get_context(session_id)
    
    # 4. 组装 Prompt
    return f"""
{SYSTEM_PROMPT}

## 参考资料
{format_docs(docs)}

## 相关记忆
{format_memories(memories)}

## 对话历史
{history}

## 用户问题
{query}
"""
```

---

## 8. API 设计

### 8.1 聊天接口

```
POST /api/chat
Body: {
  "session_id": "sess_abc123",
  "knowledge_base_id": "kb_xyz",
  "message": "千问模型有哪些版本？",
  "stream": true
}
Response: SSE stream

POST /api/chat/sessions          → 创建会话
GET  /api/chat/sessions          → 会话列表
GET  /api/chat/sessions/{id}     → 会话历史
DELETE /api/chat/sessions/{id}   → 删除会话
```

### 8.2 知识库接口

```
POST   /api/knowledge-bases                     → 创建知识库
GET    /api/knowledge-bases                     → 知识库列表
GET    /api/knowledge-bases/{id}                → 知识库详情
DELETE /api/knowledge-bases/{id}                → 删除知识库

POST   /api/knowledge-bases/{id}/documents      → 上传文档
GET    /api/knowledge-bases/{id}/documents      → 文档列表
DELETE /api/knowledge-bases/{id}/documents/{d}  → 删除文档
POST   /api/knowledge-bases/{id}/reindex        → 重新索引
```

### 8.3 系统接口

```
GET /api/health                  → 健康检查
GET /api/stats                   → 系统统计
```

---

## 9. 前端页面设计

### 9.1 页面结构

```
┌─────────────────────────────────────────────────────────────┐
│  顶部导航栏                        [设置] [用户]             │
├────────────┬────────────────────────────────────────────────┤
│            │                                                │
│  侧边栏    │              主内容区                           │
│            │                                                │
│  📚 知识库  │   ┌──────────────────────────────────────────┐ │
│  ├─ 知识库A │   │                                          │ │
│  ├─ 知识库B │   │           聊天界面                        │ │
│  └─ + 新建  │   │                                          │ │
│            │   │   用户: 千问有哪些版本？                    │ │
│  💬 对话    │   │                                          │ │
│  ├─ 会话1   │   │   助手: 通义千问目前有以下版本...           │ │
│  ├─ 会话2   │   │         [来源: 产品手册.pdf P12]           │ │
│  └─ + 新建  │   │                                          │ │
│            │   │   ┌────────────────────────────┐          │ │
│  ⚙️ 设置   │   │   │ 输入消息...        [发送]  │          │ │
│            │   │   └────────────────────────────┘          │ │
│            │   └──────────────────────────────────────────┘ │
├────────────┴────────────────────────────────────────────────┤
│  状态栏：连接状态 · 知识库: 知识库A · 模型: qwen-plus        │
└─────────────────────────────────────────────────────────────┘
```

### 9.2 知识库管理页面

```
┌──────────────────────────────────────────────────────────────┐
│  知识库管理                                    [+ 新建知识库]  │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─ 产品知识库 ──────────────────────────────────────────┐   │
│  │  📄 产品手册.pdf      42 chunks   [删除] [重新索引]    │   │
│  │  📄 FAQ.md            18 chunks   [删除] [重新索引]    │   │
│  │  📄 API文档.docx      65 chunks   [删除] [重新索引]    │   │
│  │                                                       │   │
│  │  [上传文档]  [重新索引全部]  [删除知识库]               │   │
│  │  统计: 3 个文档 · 125 个 chunks · 向量维度 1024        │   │
│  └───────────────────────────────────────────────────────┘   │
│                                                              │
│  ┌─ 内部资料 ────────────────────────────────────────────┐   │
│  │  ...                                                  │   │
│  └───────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
```

---

## 10. 项目目录结构

```
Lab-AI-Assistant/
├── docs/                          # 文档
│   └── RAG-AI-Assistant-技术设计文档.md
│
├── backend/                       # 后端
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                # FastAPI 入口
│   │   ├── config.py              # 配置管理
│   │   ├── api/                   # API 路由
│   │   │   ├── chat.py
│   │   │   ├── knowledge_base.py
│   │   │   └── documents.py
│   │   ├── core/                  # 核心业务逻辑
│   │   │   ├── rag_pipeline.py    # RAG 主流程
│   │   │   ├── document_loader.py # 文档解析
│   │   │   ├── chunker.py         # 文本分块
│   │   │   ├── embedder.py        # 向量化
│   │   │   ├── retriever.py       # 检索
│   │   │   └── reranker.py        # 重排序
│   │   ├── memory/                # 记忆系统
│   │   │   ├── short_term.py
│   │   │   ├── long_term.py
│   │   │   └── memory_manager.py
│   │   ├── llm/                   # LLM 封装
│   │   │   ├── qwen.py            # 千问 API 调用
│   │   │   └── prompt_templates.py
│   │   ├── store/                 # 存储层
│   │   │   ├── vector_store.py    # ChromaDB 封装
│   │   │   └── db.py              # SQLite ORM
│   │   └── models/                # 数据模型
│   │       ├── schemas.py         # Pydantic schemas
│   │       └── database.py        # SQLAlchemy models
│   ├── tests/
│   ├── requirements.txt
│   └── Dockerfile
│
├── frontend/                      # 前端
│   ├── src/
│   │   ├── views/
│   │   │   ├── ChatView.vue       # 聊天页面
│   │   │   └── KnowledgeBase.vue  # 知识库管理
│   │   ├── components/
│   │   │   ├── ChatMessage.vue
│   │   │   ├── DocumentUpload.vue
│   │   │   └── SideNav.vue
│   │   ├── stores/                # Pinia 状态管理
│   │   ├── api/                   # API 调用封装
│   │   └── router/
│   ├── package.json
│   └── Dockerfile
│
├── data/                          # 运行时数据 (gitignore)
│   ├── chroma/                    # ChromaDB 持久化
│   ├── uploads/                   # 上传的原始文档
│   └── app.db                     # SQLite 数据库
│
├── docker-compose.yml             # 可选容器化部署
├── .env.example                   # 环境变量模板
├── .gitignore
└── README.md
```

---

## 11. 开发路线图

### Phase 1 — MVP（最小可用版本）
- [x] 项目初始化
- [ ] 后端 FastAPI 框架搭建
- [ ] DashScope SDK 集成（千问对话 + Embedding）
- [ ] 基础文档上传、解析、分块、向量化
- [ ] ChromaDB 存储与检索
- [ ] 基础 RAG Pipeline（检索 → 拼装 → 生成）
- [ ] 简单的聊天 API（非流式）

### Phase 2 — 核心功能完善
- [ ] 流式输出 (SSE)
- [ ] 多知识库管理 CRUD
- [ ] 短期记忆（对话历史 + 滑动窗口）
- [ ] Prompt 优化与引用来源标注
- [ ] 前端聊天界面

### Phase 3 — 高级功能
- [ ] Query Rewrite（查询改写）
- [ ] Re-ranking（重排序）
- [ ] 混合检索（关键词 + 向量，RRF 融合）
- [ ] 长期记忆系统
- [ ] 前端知识库管理界面
- [ ] 文档重新索引

### Phase 4 — 生产化
- [ ] 用户认证与权限
- [ ] 日志与监控
- [ ] 性能优化（批量 Embedding、缓存）
- [ ] Docker 部署
- [ ] API 限流与错误处理

---

## 12. 环境变量配置

```bash
# .env.example

# DashScope (阿里百炼)
DASHSCOPE_API_KEY=sk-xxxxxxxxxxxxx

# 模型配置
LLM_MODEL=qwen-plus
EMBEDDING_MODEL=text-embedding-v3

# ChromaDB
CHROMA_PERSIST_DIR=./data/chroma

# 应用配置
APP_HOST=0.0.0.0
APP_PORT=8000
DATABASE_URL=sqlite:///./data/app.db

# RAG 参数
CHUNK_SIZE=512
CHUNK_OVERLAP=64
RETRIEVAL_TOP_K=5
SIMILARITY_THRESHOLD=0.3
MAX_CONTEXT_TOKENS=3000
```

---

## 附录 A：关键技术决策记录

| 决策                   | 选择        | 理由                                                     |
| ---------------------- | ----------- | -------------------------------------------------------- |
| 向量数据库             | ChromaDB    | 零配置、本地持久化、Python 原生，MVP 阶段最合适          |
| Embedding 模型         | DashScope   | 与千问同生态，避免跨平台延迟，中文效果好                  |
| 前端框架               | Vue 3       | 上手快、Element Plus 组件丰富、适合管理后台风格           |
| 后端框架               | FastAPI     | 异步原生、自动文档、类型安全                              |
| 分块策略               | 递归字符分割 | 通用性好，先跑通再优化                                    |
| 记忆存储               | ChromaDB 复用| 向量检索天然适合记忆召回，无需引入额外存储                |

---

## 附录 B：风险与缓解

| 风险                         | 缓解措施                                          |
| ---------------------------- | ------------------------------------------------- |
| 千问 API 限流/不稳定         | 重试机制 + 指数退降 + 备用模型切换                |
| 文档解析格式兼容性差         | 四级兜底链：专用解析器 → unstructured → 按纯文本读 → 标记 failed 并提示转格式（详见 Phase 1-04 开发文档 §3.2）|
| 检索质量差导致回答不准确     | 引入混合检索（关键词 + 向量，Phase 3-06）、Re-rank、调整 chunk 策略     |
| 长对话 Token 超限            | 滑动窗口 + 摘要压缩，动态 Token 预算管理          |
| ChromaDB 大规模性能下降      | 架构抽象向量存储接口，可迁移到 Milvus/Qdrant      |
