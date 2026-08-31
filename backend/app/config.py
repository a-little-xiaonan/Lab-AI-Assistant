"""全局配置：唯一读环境变量的地方，其他模块一律 `from app.config import settings`。

注意：本机框架版 Python 的 CA 证书缺失（urllib 直连会 SSL 报错），
在 import 一切网络库之前引导 certifi 的证书路径，保证 unstructured 等
库的运行时下载（spacy/nltk 模型）不会因证书问题失败。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("SSL_CERT_DIR", Path(certifi.where()).parent.as_posix())

from pydantic_settings import BaseSettings, SettingsConfigDict  # noqa: E402

logger = logging.getLogger(__name__)

# 项目根目录：backend/app/config.py -> parents[2]
ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ----- DashScope（通义千问）-----
    dashscope_api_key: str = ""          # 启动时只 warning，调用时才校验（用户后填）
    llm_model: str = "qwen-plus"
    llm_model_fallback: str = "qwen-max"  # 备用模型（预留，主模型失败时切换）
    embedding_model: str = "text-embedding-v3"
    llm_timeout: int = 60
    llm_stream_timeout: int = 300  # 流式超时放宽：长回答生成期间无单个块超时风险

    # ----- 服务 -----
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    # ----- 存储（相对路径统一锚定项目根目录，与运行目录无关）-----
    chroma_persist_dir: str = "./data/chroma"
    database_url: str = "sqlite:///./data/app.db"

    # ----- 文档处理 -----
    chunk_size: int = 512
    chunk_overlap: int = 64
    max_upload_size_mb: int = 50

    # ----- 检索 -----
    retrieval_top_k: int = 5
    # 实测标定（2026-08-30，text-embedding-v3）：无关中文文本噪声地板 ~0.35-0.41，
    # 相关命中 0.5+，故从设计文档建议值 0.3 上调到 0.45，防噪声混入引用
    similarity_threshold: float = 0.45
    max_context_tokens: int = 3000

    # ----- 混合检索（Phase 3-06）-----
    hybrid_retrieval_enabled: bool = True   # 总开关；false → 行为与 Phase 2 完全一致
    hybrid_vector_top_k: int = 10           # 向量侧每查询 top-k
    hybrid_keyword_top_k: int = 10          # 关键词侧 top-k
    hybrid_fusion_candidates: int = 20      # RRF 融合后候选集规模（供重排）
    keyword_use_rewritten: bool = False     # 关键词侧是否也用改写查询（默认关：词面精确优先）
    keyword_jieba_dict: str = ""            # jieba 自定义词典路径（相对项目根；空 = 不加载）

    # ----- 查询改写（Phase 3-01）-----
    rewrite_enabled: bool = True
    rewrite_query_count: int = 3            # 改写总数上限（含原查询）
    rewrite_model: str = ""                 # 改写用模型；空 → llm_model（可用独立模型验证降级）

    # ----- 重排（Phase 3-02）-----
    rerank_enabled: bool = False            # 默认关（评估后定）；开启后 RRF 候选集精排
    rerank_top_n: int = 5                   # 重排后进上下文的条数
    reranker_type: str = "dashscope"        # dashscope / local（本地 Cross-Encoder 需自行装 transformers）
    rerank_model: str = "gte-rerank-v2"     # DashScope 重排模型（实测 2026-08-31：gte-rerank 403 未开通，v2/qwen3-rerank 可用）

    # ----- 短期记忆（Phase 2-03）-----
    history_max_turns: int = 10          # 窗口轮数：超过 2 倍触发摘要压缩
    history_max_tokens: int = 4000       # 历史段 token 预算（超限丢最旧）
    memory_max_instances: int = 200      # 进程内记忆实例上限（LRU 淘汰，DB 是源可重建）
    memory_idle_ttl_seconds: int = 3600  # 实例空闲过期时间

    # ----- 会话保留（自动清理）-----
    session_retention_days: int = 3            # 超过 N 天未更新的活跃会话 → 逻辑删除
    session_purge_days: int = 30               # 已逻辑删除超过 N 天 → 物理删除（含消息与长期记忆）
    session_cleanup_interval_hours: int = 6    # 定时清理间隔（小时；启动时也清一次）

    # ----- 长期记忆（Phase 3-03）-----
    memory_confidence_threshold: float = 0.7  # 提取置信度阈值：低于此值的记忆不入库（唯一防线）
    memory_recall_top_k: int = 3              # 每轮召回记忆条数上限（防 prompt 膨胀）

    @property
    def chroma_dir(self) -> Path:
        p = Path(self.chroma_persist_dir)
        return p if p.is_absolute() else ROOT / p

    @property
    def sqlite_path(self) -> Path:
        """从 sqlite:///./data/app.db 提取并锚定到项目根目录。"""
        url = self.database_url
        if url.startswith("sqlite:///"):
            p = Path(url.removeprefix("sqlite:///"))
            return p if p.is_absolute() else ROOT / p
        return ROOT / "data" / "app.db"

    @property
    def database_url_resolved(self) -> str:
        """SQLite 相对路径锚定项目根；MySQL 等 URL 原样透传。"""
        url = self.database_url
        if url.startswith("sqlite:///"):
            return f"sqlite:///{self.sqlite_path}"
        return url

    @property
    def uploads_dir(self) -> Path:
        return ROOT / "data" / "uploads"


def get_settings() -> Settings:
    return Settings()


settings = get_settings()

if not (ROOT / ".env").exists():
    logger.warning(
        "未找到 .env 文件（%s）。请复制 .env.example 为 .env 并填入 DASHSCOPE_API_KEY，"
        "否则向量化与问答接口会返回 api_key_missing 错误。", ROOT / ".env"
    )


def ensure_data_dirs() -> None:
    """启动时创建运行时目录（data/chroma、data/uploads、sqlite 父目录）。"""
    for d in (settings.chroma_dir, settings.uploads_dir, settings.sqlite_path.parent):
        d.mkdir(parents=True, exist_ok=True)
