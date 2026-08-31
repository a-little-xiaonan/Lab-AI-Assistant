# Phase 1 · 02 FastAPI 框架搭建 — 开发文档

> **所属阶段**：Phase 1 — MVP
> **路线图条目**：§11 Phase 1 第 2 项「后端 FastAPI 框架搭建」
> **参考章节**：§3 技术栈（后端框架）· §8 API 设计 · §12 环境变量
> **前置依赖**：Phase 1-01 项目初始化
> **状态**：待开发

## 1. 目标与范围

后端服务骨架可启动：配置管理、路由注册、CORS、健康检查就绪，为后续所有 API 提供宿主。

**范围**：不含任何业务逻辑（聊天、知识库均为空路由占位，后续阶段填充）。

## 2. 任务拆解

- [ ] 安装依赖：`fastapi`、`uvicorn[standard]`、`pydantic-settings`、`python-multipart`（文件上传预装）
- [ ] `app/config.py`：用 pydantic-settings 定义 `Settings`，读取 `.env`（字段对齐 §12），导出单例 `settings`
- [ ] `app/main.py`：创建 `FastAPI` 实例，注册路由，配置 CORS（允许开发地址 `http://localhost:5173`）
- [ ] `app/api/health.py`：`GET /api/health` 健康检查
- [ ] `app/api/stats.py`：`GET /api/stats` 系统统计（MVP 先返回静态占位）
- [ ] `app/api/chat.py`、`knowledge_base.py`、`documents.py`：建立空路由文件（router 注册，接口后续阶段填充）
- [ ] 启动脚本：`Makefile` 或 README 记录 `uvicorn app.main:app --reload --port 8000`

## 3. 设计要点

- **配置单一入口**：`config.py` 是唯一读取环境变量的地方，其他模块一律 `from app.config import settings`，禁止散落 `os.getenv`
- **CORS**：开发期放开 `localhost:5173`；生产收敛为白名单（Phase 4）
- **路由前缀**：统一 `api/`，后续阶段按 §8 补齐端点
- `/api/health` 返回 `{"status": "ok", "version": "0.1.0"}`，供前端状态栏与运维探活使用
- 空路由文件提前建好，避免后续每步都要动 `main.py` 的 include_router 列表

## 4. 涉及文件

```
backend/app/
├── main.py            # FastAPI 实例 + CORS + 路由注册
├── config.py          # pydantic-settings 配置单例
└── api/
    ├── __init__.py
    ├── health.py      # GET /api/health
    ├── stats.py       # GET /api/stats
    ├── chat.py        # 空路由占位（Phase 1-07）
    ├── knowledge_base.py  # 空路由占位（Phase 2-02）
    └── documents.py   # 空路由占位（Phase 1-04 起使用）
```

## 5. 验收标准

- [ ] `uvicorn app.main:app --reload` 启动无报错
- [ ] `GET /api/health` → 200 `{"status": "ok", ...}`
- [ ] `GET /docs`（OpenAPI 文档）可访问，能看到全部已注册路由
- [ ] 修改 `.env` 中 `APP_PORT` 后重启生效（验证配置读取链路）
- [ ] 前端 `localhost:5173` 发起请求无 CORS 报错（可用 curl 带 Origin 头验证）

## 6. 风险与注意事项

- **端口冲突**：8000 被占用时用 `lsof -i :8000` 排查，或改 `.env` 的 `APP_PORT`
- **配置缺失**：`.env` 不存在时服务应能启动但给出 warning（敏感配置如 `DASHSCOPE_API_KEY` 留到 Phase 1-03 校验），避免启动即崩
- CORS 配置错误在浏览器端表现为"跨域"，排查时先看响应头 `Access-Control-Allow-Origin`
