"""检索主题配置：管理员标注与查询主题提示共用，mtime 变化自动热加载。

配置失效时返回空主题而非抛异常，定向检索即可自然降级为全局检索。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config import ROOT, settings

logger = logging.getLogger(__name__)


class RetrievalTopics:
    def __init__(self) -> None:
        self._mtime: float | None = None
        self._topics: dict[str, dict] = {}

    def _path(self) -> Path:
        configured = Path(settings.retrieval_topics_file)
        if not configured.is_absolute():
            configured = ROOT / configured
        return configured if configured.exists() else ROOT / "retrieval_topics.example.json"

    def _load(self) -> None:
        path = self._path()
        try:
            mtime = path.stat().st_mtime
            if self._mtime == mtime:
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
            topics: dict[str, dict] = {}
            for item in raw.get("topics", []):
                code = str(item.get("code", "")).strip()
                name = str(item.get("name", "")).strip()
                aliases = [str(v).strip() for v in item.get("aliases", []) if str(v).strip()]
                if code and name and code.replace("_", "").isalnum():
                    topics[code] = {"code": code, "name": name, "aliases": aliases}
            self._topics = topics
            self._mtime = mtime
            logger.info("检索主题配置已加载：%d 个主题（%s）", len(topics), path.name)
        except Exception:
            logger.exception("检索主题配置无效，本次禁用主题定向检索")
            self._topics = {}
            self._mtime = None

    def all(self) -> list[dict]:
        self._load()
        return list(self._topics.values())

    def valid_codes(self, codes: list[str]) -> list[str]:
        self._load()
        seen: set[str] = set()
        return [c for c in codes if c in self._topics and not (c in seen or seen.add(c))]

    def hints_for(self, text: str) -> list[str]:
        """从主题别名做轻量匹配；这里只是提示，绝不阻断全局召回。"""
        self._load()
        normalized = text.casefold()
        hits = []
        for code, topic in self._topics.items():
            terms = [topic["name"], *topic["aliases"]]
            if any(term.casefold() in normalized for term in terms if term):
                hits.append(code)
        return hits


retrieval_topics = RetrievalTopics()
