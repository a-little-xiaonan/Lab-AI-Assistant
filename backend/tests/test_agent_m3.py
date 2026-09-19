"""M3 离线验收：不连接百炼服务，也不读取真实网页。"""
import json
import socket
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.config import settings
from app.core.agent.models import ToolContext
from app.core.agent.runtime import ExecutionBudget
from app.services.mcp.bailian import BailianMcpClient, McpTool
from app.services.web.page_reader import WebPage
from app.services.web.url_policy import UrlPolicyError, validate_public_url
from app.tools import registry
from app.tools.web import search as web_tool


def ctx(query="查询公开新闻", session_id="m3"):
    return ToolContext((), ExecutionBudget(time.monotonic() + 5), "Asia/Shanghai",
                       session_id=session_id, current_query=query)


def call(name, arguments, call_id="m3"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def test_web_tools_hidden_by_default(monkeypatch):
    monkeypatch.setattr(settings, "web_search_enabled", False)
    monkeypatch.setattr(settings, "web_read_enabled", False)
    names = {item["function"]["name"] for item in registry.schemas()}
    assert "web_search" not in names and "read_webpage" not in names


def test_url_policy_rejects_private_ip():
    def resolver(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
    with pytest.raises(UrlPolicyError):
        validate_public_url("https://localhost/private", resolve=resolver)


def test_mcp_client_discovers_and_calls_without_network(monkeypatch):
    seen = {}
    class Session:
        async def initialize(self): pass
        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="web_search", description="公开搜索",
                inputSchema={"properties": {"query": {"type": "string"}}})])
        async def call_tool(self, name, arguments):
            seen["call"] = (name, arguments)
            return SimpleNamespace(isError=False, content=[SimpleNamespace(text="[]")])
    @asynccontextmanager
    async def connector(url, headers):
        seen["url"], seen["auth"] = url, headers["Authorization"]
        yield Session()
    monkeypatch.setattr(settings, "bailian_mcp_web_search_tool", "")
    client = BailianMcpClient(url="https://example.test/mcp", api_key="unit-key", connector=connector)
    tool, result = client.search("公开信息")
    assert tool.name == "web_search" and result == "[]"
    assert seen["call"] == ("web_search", {"query": "公开信息"})
    assert seen["auth"] == "Bearer unit-key"


def test_search_returns_leads_only_and_session_handle(monkeypatch):
    monkeypatch.setattr(settings, "web_search_enabled", True)
    monkeypatch.setattr(web_tool, "validate_public_url", lambda url: SimpleNamespace(url=url))
    class FakeClient:
        def search(self, query):
            return McpTool("web_search", "", {"properties": {"query": {}}}), json.dumps([
                {"title": "公开页面", "url": "https://example.com/a", "snippet": "线索"}])
    monkeypatch.setattr(web_tool, "BailianMcpClient", FakeClient)
    context = ctx()
    result = registry.execute(call("web_search", {"query": "公开信息", "year": 2026}), context)
    assert result.status == "ok"
    lead = result.data["leads"][0]
    assert lead["evidence_status"] == "lead" and lead["handle"] in context.resource_handles
    assert "site:" not in result.model_dump_json()


def test_read_requires_issued_handle_or_literal_user_url(monkeypatch):
    monkeypatch.setattr(settings, "web_read_enabled", True)
    monkeypatch.setattr(web_tool.PageReader, "read", lambda self, url: WebPage(url, "标题", "已核实正文", "text/html", False))
    context = ctx("请解读 https://example.com/report")
    direct = registry.execute(call("read_webpage", {"url": "https://example.com/report"}), context)
    assert direct.status == "ok" and direct.data["evidence_status"] == "supported"
    denied = registry.execute(call("read_webpage", {"url": "https://other.example/"}, "other"), context)
    assert denied.error.code == "url_not_in_user_request"
    context.resource_handles["webres_ok"] = {"kind": "web_lead", "session_id": "m3", "url": "https://example.com/a"}
    by_handle = registry.execute(call("read_webpage", {"handle": "webres_ok"}, "handle"), context)
    assert by_handle.status == "ok"


def test_internal_query_is_not_sent_to_mcp(monkeypatch):
    monkeypatch.setattr(settings, "web_search_enabled", True)
    result = registry.execute(call("web_search", {"query": "查询内部知识库"}), ctx())
    assert result.status == "needs_input"
