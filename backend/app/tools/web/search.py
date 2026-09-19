"""公开联网能力：受策略限制的搜索和网页阅读工具。"""
from __future__ import annotations

import json
import re
from secrets import token_urlsafe

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.agent.models import ToolContext, ToolError, ToolResult
from app.services.mcp.bailian import BailianMcpClient, BailianMcpError
from app.services.web.page_reader import PageReadError, PageReader
from app.services.web.url_policy import UrlPolicyError, validate_public_url

_BLOCKED = ("内部资料", "内部知识", "知识库", "员工名单", "成员隐私", "手机号", "身份证", "密码", "访问令牌")
_URL = re.compile(r"https?://[^\\s<>\"]+", re.I)


class WebSearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=300)
    year: int | None = Field(default=None, ge=2000, le=2100)
    domain: str | None = Field(default=None, min_length=1, max_length=253)

    @model_validator(mode="after")
    def public_only(self):
        if any(word in self.query for word in _BLOCKED):
            raise ValueError("不能将内部或敏感信息发送到外部搜索服务")
        if self.domain and ("/" in self.domain or ":" in self.domain or not re.fullmatch(r"[A-Za-z0-9.-]+", self.domain)):
            raise ValueError("domain 必须是域名")
        return self


class WebReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    handle: str | None = Field(default=None, max_length=100)
    url: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def one_target(self):
        if bool(self.handle) == bool(self.url):
            raise ValueError("必须提供一个搜索结果句柄或用户给出的链接")
        return self


def _search_text(args: WebSearchArgs) -> str:
    parts = [args.query.strip()]
    if args.year:
        parts.append(str(args.year))
    if args.domain:
        parts.append(f"site:{args.domain}")
    return " ".join(parts)[:500]


def _leads(raw: str) -> list[dict]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = None
    found: list[dict] = []
    def visit(item):
        if isinstance(item, dict):
            url = next((item.get(k) for k in ("url", "link", "href") if isinstance(item.get(k), str)), None)
            if url:
                try:
                    safe = validate_public_url(url)
                    title = next((item.get(k) for k in ("title", "name") if isinstance(item.get(k), str)), "")
                    snippet = next((item.get(k) for k in ("snippet", "summary", "description") if isinstance(item.get(k), str)), "")
                    found.append({"url": safe.url, "title": title[:300], "snippet": snippet[:600]})
                except UrlPolicyError:
                    pass
            for child in item.values(): visit(child)
        elif isinstance(item, list):
            for child in item: visit(child)
    visit(value)
    if not found:
        for url in _URL.findall(raw):
            try: found.append({"url": validate_public_url(url).url, "title": "", "snippet": ""})
            except UrlPolicyError: pass
    unique = {item["url"]: item for item in found}
    return list(unique.values())[:5]


def web_search(args: WebSearchArgs, ctx: ToolContext) -> ToolResult:
    try:
        tool, raw = BailianMcpClient().search(_search_text(args))
        leads = _leads(raw)
    except BailianMcpError as exc:
        return ToolResult(status="failed", error=ToolError(code=exc.code, message=exc.message))
    if not leads:
        return ToolResult(status="no_data", data={"query": args.query, "leads": []},
                          provenance={"provider": "bailian_mcp_web_search"})
    for lead in leads:
        handle = f"webres_{token_urlsafe(18)}"
        ctx.resource_handles[handle] = {"kind": "web_lead", "session_id": ctx.session_id, "url": lead["url"]}
        lead["handle"] = handle
        lead["evidence_status"] = "lead"
    return ToolResult(status="ok", data={"query": args.query, "leads": leads},
                      provenance={"provider": "bailian_mcp_web_search", "tool": tool.name,
                                  "evidence_status": "lead"})


def read_webpage(args: WebReadArgs, ctx: ToolContext) -> ToolResult:
    if args.handle:
        item = ctx.resource_handles.get(args.handle)
        if not item or item.get("kind") != "web_lead" or item.get("session_id") != ctx.session_id:
            return ToolResult(status="unsupported", error=ToolError(code="invalid_web_handle", message="网页句柄无效或不属于本次会话。"))
        url = item["url"]
    else:
        url = args.url or ""
        if url not in ctx.current_query:
            return ToolResult(status="unsupported", error=ToolError(code="url_not_in_user_request", message="只能读取用户本轮明确提供的链接。"))
    try:
        page = PageReader().read(url)
    except PageReadError as exc:
        return ToolResult(status="failed", error=ToolError(code=exc.code, message=exc.message))
    if not page.text:
        return ToolResult(status="no_data", data={"url": page.url, "reason": "empty_page"})
    return ToolResult(status="ok", data={"url": page.url, "title": page.title, "text": page.text,
                                           "evidence_status": "supported"},
                      provenance={"provider": "public_web_page", "url": page.url,
                                  "content_type": page.content_type, "evidence_status": "supported"},
                      truncated=page.truncated)
