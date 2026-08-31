"""术语归一化映射表（Phase 3-01 补强）：口语表达 → 标准术语的规则兜底。

设计要点（对齐第 2 点"术语归一化映射与规则兜底"）：
- 配置文件：data/term_aliases.json（相对项目根，gitignored，用户本地编辑）；
  不存在时回退到项目根 term_aliases.example.json（开箱即用）
- 格式：{"标准术语": ["别名1", "别名2", ...]}；查询命中任一别名 → 生成
  该别名替换为标准术语的变体（大小写不敏感；别名 `_` 开头或值非数组被忽略）
- 挂接点：hybrid_retriever 关键词侧（BM25 词面匹配，最需要口语→术语的桥接）；
  向量侧由 LLM 改写承担术语化，不重复展开
- 热更新：每次 expand 按 mtime 检查，用户改文件不用重启
- 失败语义：文件缺失/损坏 → 空表 + 日志，检索链路不中断（兜底本身也是兜底）
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from app.config import ROOT, settings

logger = logging.getLogger(__name__)


class TermAliases:
    def __init__(self) -> None:
        self._table: dict[str, list[str]] = {}
        self._mtime: float | None = None
        self._loaded_path: Path | None = None

    def _paths(self) -> tuple[Path, Path]:
        f = Path(settings.term_aliases_file)
        if not f.is_absolute():
            f = ROOT / f
        return f, ROOT / "term_aliases.example.json"

    def _load(self) -> None:
        """按 mtime 热重载（幂等；异常 → 空表，调用方已 try/except 包裹）。"""
        path, example = self._paths()
        src = path if path.exists() else example
        try:
            mtime = src.stat().st_mtime
        except OSError:
            self._table, self._mtime, self._loaded_path = {}, None, None
            return
        if src == self._loaded_path and mtime == self._mtime:
            return
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("术语映射表解析失败：%s（按空表处理）", src)
            self._table, self._mtime, self._loaded_path = {}, None, None
            return
        table: dict[str, list[str]] = {}
        for term, aliases in data.items():
            # 过滤 `_` 开头键（JSON 无注释，用下划线键写说明）与非数组值
            if not isinstance(term, str) or term.startswith("_") or not isinstance(aliases, list):
                continue
            cleaned = [a for a in aliases if isinstance(a, str) and a.strip()]
            if term.strip() and cleaned:
                table[term.strip()] = cleaned
        self._table, self._mtime, self._loaded_path = table, mtime, src
        if table:
            logger.info("术语映射表加载：%s（%d 条目）", src, len(table))

    def expand(self, query: str) -> list[str]:
        """返回 [原查询, ...术语变体]（去重；未命中返回 [原查询]；上限 term_alias_max_expansions）。"""
        if not settings.term_aliases_enabled or not query.strip():
            return [query]
        try:
            self._load()
        except Exception:
            logger.exception("术语映射表加载失败，按空表处理")
            return [query]
        low = query.casefold()
        variants: list[str] = []
        for term, aliases in self._table.items():
            if len(variants) >= settings.term_alias_max_expansions:
                break
            hits = [a for a in aliases if a.casefold() in low]
            if not hits:
                continue
            v = query
            for a in hits:
                v = re.sub(re.escape(a), term, v, flags=re.IGNORECASE)
            if v != query:
                variants.append(v)
        if not variants:
            return [query]
        # 去重（忽略大小写与首尾空白）：变体可能互相等价（如 qwen / Qwen → 同一标准词）
        seen = {query.casefold().strip()}
        out = [query]
        for v in variants:
            key = v.casefold().strip()
            if key not in seen:
                seen.add(key)
                out.append(v)
            if len(out) - 1 >= settings.term_alias_max_expansions:
                break
        logger.info("术语映射展开：%d 条变体（%s）", len(out) - 1, out[1:])
        return out


term_aliases = TermAliases()  # 模块级单例
