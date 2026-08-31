# Phase 1 · 04 文档处理 Pipeline — 开发文档

> **所属阶段**：Phase 1 — MVP
> **路线图条目**：§11 Phase 1 第 4 项「基础文档上传、解析、分块、向量化」
> **参考章节**：§4.1 文档处理 Pipeline · §5 向量数据库设计 · 附录 B（解析格式兼容性）
> **前置依赖**：Phase 1-03 DashScope SDK 集成（向量化环节）
> **状态**：待开发

> 注：路线图条目包含「向量化」，但本文档只覆盖 **上传 → 解析 → 清洗 → 分块** 四个环节（产出 chunk 列表 + 元数据）；「向量化 → 入库」在 Phase 1-05（ChromaDB 存储与检索）中实现。

## 1. 目标与范围

支持 PDF / DOCX / MD / TXT / PPTX / XLSX 等主流格式的文档上传与解析（PPT/Excel/HTML 走 unstructured），**未知格式经四级兜底链处理**（可读则入库，真二进制则优雅拒绝）。输出规范化的 chunk 列表及元数据（对齐 §4.1），并保存原始文件到 `data/uploads/`。

## 2. 任务拆解

- [ ] 安装解析依赖：`pymupdf`（PDF）、`python-docx`（DOCX）、`unstructured`（PPT/Excel/HTML/未知格式；其依赖自带 python-pptx、openpyxl）
- [ ] `app/core/document_loader.py`：格式分发（见 §3.1）
  - `.pdf` → PyMuPDF 逐页提取，记录 `page`
  - `.md` / `.txt` → 直读（优先 UTF-8，失败回退 GBK）
  - `.docx` → python-docx 合并段落，尽量保留章节信息
  - `.pptx` / `.xlsx` / `.html` → unstructured 解析（输出元素列表，含 slide/sheet/表格结构）
  - 其余 / 解析失败 → 四级兜底链（见 §3.2）
- [ ] `app/core/cleaner.py`：文本清洗 —— 去除乱码字符（非法 Unicode）、合并断行、去除页眉页脚（正则 + 启发式）
- [ ] `app/core/chunker.py`：按格式选择切块策略（见 §3.3），超长块递归字符分割 `chunk_size=512, overlap=64`（§4.1）
- [ ] 每个 chunk 生成完整元数据（§3.4 schema）
- [ ] 上传保存：`data/uploads/{kb_id}/{doc_id}/原始文件名`，`doc_id` 用 UUID（或内容 hash 去重）
- [ ] 单元测试：`tests/test_chunker.py`（中文切分不破句、overlap 生效、元数据齐全、PPT/Excel 样例）

## 3. 设计要点

### 3.1 格式分发（谁读什么）

| 格式 | 解析器 | 理由 |
| ---- | ------ | ---- |
| PDF | PyMuPDF | 中文文本层抽取得快且准 |
| DOCX | python-docx | 轻量可控，段落/表格结构够用 |
| MD / TXT | 直读 | 零成本，没必要绕一圈 |
| PPTX / XLSX / HTML / 其他已知格式 | unstructured | 结构识别红利：slide 元素、表格转 HTML |
| 未知格式 | 四级兜底链（3.2） | 见下 |

### 3.2 四级兜底链（附录 B 风险缓解的具体化）

```
L1  已知格式 → 专用解析器（上表）
                 │ 未命中 或 解析失败
L2  unstructured 全格式尝试（EPUB / RTF / ODT / 图片等）
                 │ 也失败
L3  文本试探 → 按 UTF-8 / GBK 解码，可读就当 TXT 处理
                 （JSON / XML / CSV / 日志 / 源码等"未知格式"其实多是文本）
                 │ 仍失败（真二进制）
L4  优雅拒绝 → 文档标记 failed，error_message 写明：
                 "暂不支持该格式（.xyz），请转为 PDF / DOCX / MD / TXT 后上传"
                 不中断上传流程、不产生脏数据
```

- 兜底不是"硬撑"而是"优雅拒绝"：L4 给出可读错误信息（配合 Phase 2-02 文档状态机的 `failed` 状态展示）
- 未知格式的元数据退化：无 page/slide/sheet 概念 → 只有 `source_file`，引用标注退化为文件名（可接受）

### 3.3 分块策略：结构分块 + 固定分块组合

两种方法职责不同、缺一不可，组合为固定顺序的两阶段流程（所有格式统一）：

```
文档
 → ① 结构分块：按格式的结构特征切出"结构块"（语义单元，可能 150 字也可能 2000 字）
 → ② 固定分块：每个结构块用统一的 512/64 算法规整到预算内
 → chunk 列表（挂结构块归属的元数据）
```

**① 结构分块（找边界）**：块从哪切，由格式决定

| 格式 | 结构边界 |
| ---- | ---- |
| PDF | 页内段落（跨页段落合并，页只作元数据）|
| Word | Heading 标题节（未用样式的文档退化为按段落）|
| PPT | 一个 slide（不跨 slide 合并）|
| MD | `#` 标题层级 |
| TXT | 段落（`\n\n`）|
| Excel | sheet / 行区块（~50 行/块，表头重复）|

**② 固定分块（规整大小）**：方法全格式共用一份，参数固定

```
fixed_split(block, size=512, overlap=64)
  分隔符层级依次尝试（递归字符分割）：
    L1 空行 \n\n    → 段落级切分
    L2 换行 \n      → 行级切分
    L3 句子标点     → 中文 。！？ / 英文 . ! ?
    L4 字符         → 兜底硬切（避免无限递归）
  每两个相邻块之间保留 overlap=64 字重叠
```

- 结构块 ≤ 512：整块保留，不再切（块小不硬凑；碎片合并列为后置增强）
- 结构块 > 512：按上述层级递归切到 ≤ 512
- **overlap 永不跨结构块**：重叠只发生在同一个结构块内部 —— 结构边界（slide 边界、标题节边界）永远不被重叠污染，引用出处保持干净

代码形态（伪代码）：

```python
def chunk_document(doc, fmt):
    units = structural_split(doc, fmt)      # ① 结构分块：按格式取边界
    chunks = []
    for unit in units:
        for seg in fixed_split(unit.text, size=512, overlap=64):  # ② 固定分块：规整
            chunks.append(Chunk(text=seg, metadata=unit.metadata))
    return chunks
```

**明确不在 MVP 范围**（后续按需增强，每项独立开关）：PDF 分栏识别、Word 视觉标题兜底（加粗大字当标题）、PPT 子块锚点（子块重复 slide 标题）、PPT 备注分区、语义分割（Embedding 相似度断句，§4.1）—— 评估基线（Phase 2-04）显示检索质量不足时再逐层加回

### 3.4 chunk 元数据 schema（§4.1 扩展版）

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

- 字段按格式**按需出现**：没有页/slide/sheet 概念的格式（如未知文本格式）只保留 `source_file` 等通用字段
- 字段命名是全链路约定：Phase 2-04 的引用标注、Phase 3-04 的前端展示都只认这些名字

### 3.5 其他要点

- **MVP 简化**：Phase 1 只有一个隐式默认知识库（`kb_default`）；多知识库隔离在 Phase 2-02 引入
- **doc_id 去重**：内容 hash 检测，重复上传返回"已存在"
- **上传白名单放开**：接受所有扩展名，由兜底链决定成败（坏格式走 L4，不会入库）；大小限制（50MB）保留
- **扫描版 PDF 无文本层**：记录 warning 并跳过（OCR 明确不在 MVP 范围；unstructured 的 OCR 能力留作后续增强）
- 输出为纯函数式接口：`load(path) -> list[Element]`、`clean(text) -> str`、`chunk(elements) -> list[Chunk]`，便于单测与复用

## 4. 涉及文件

```
backend/app/core/
├── document_loader.py   # 格式分发 + 四级兜底链
├── cleaner.py           # 文本清洗
├── chunker.py           # 按格式切块策略 + 512/64 规整
└── models.py            # Chunk 数据类（或并入 schemas.py）

backend/tests/test_chunker.py
backend/app/api/documents.py   # 上传端点（临时简单实现，Phase 2-02 完善）
```

## 5. 验收标准

- [ ] 上传 PDF / MD / TXT / DOCX / PPTX / XLSX 六种样例，均输出 chunk 列表且无抛错
- [ ] 上传未知格式的文本文件（如 `.log` / `.json`）→ 经 L3 兜底按文本入库
- [ ] 上传真二进制未知格式（如 `.exe` / `.zip`）→ 文档标记 failed，错误信息可读，上传流程不中断
- [ ] PPT 样例：chunk 数与 slide 数对应，`slide_number` 元数据正确
- [ ] Excel 样例：按 sheet/行区块切块，`sheet_name` / `row_range` 正确，表格文本可读
- [ ] 每个 chunk 元数据与 §3.4 schema 一致（按需字段）
- [ ] 约 2000 字的中文文档 → 约 4~5 个 chunk（512 字/块），overlap 生效，中文句子不被切断（抽样验证）
- [ ] 重复上传同一文件被识别（hash 去重）

## 6. 风险与注意事项

- **格式兼容性**（附录 B）：不同 PDF 排版差异大，清洗规则要保守（宁可少清洗，不可误删正文）
- **unstructured 依赖重量与版本漂移**：锁定版本；首次运行会下载 nltk 等资源，安装/启动时间变长是预期内的
- **编码问题**：TXT 常见 GBK 编码，UTF-8 失败必须回退，否则中文乱码进向量库会污染检索
- **大文件**：单文件大小限制建议 50MB（前后端同时校验），超限直接拒绝
- **Excel 大表**：几千行的 sheet 转文本 token 巨大，按行区块切块是必须项，不是优化项
- chunk 大小与 overlap 参数从 `settings` 读取（`CHUNK_SIZE` / `CHUNK_OVERLAP`），不要在代码里写死
