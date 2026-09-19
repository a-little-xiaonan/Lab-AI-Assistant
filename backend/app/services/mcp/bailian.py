"""百炼 Streamable HTTP MCP 客户端，仅允许调用已发现的联网搜索工具。"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from app.config import settings

logger = logging.getLogger(__name__)
_CACHE_SECONDS = 300


class BailianMcpError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class McpTool:
    name: str
    description: str
    input_schema: dict[str, Any]


class BailianMcpClient:
    """同步工具层使用的薄封装；I/O 由 MCP 官方 SDK 完成。

    connector 是测试注入点，生产时使用 Streamable HTTP。缓存只保存无敏感信息的工具声明。
    """
    def __init__(self, url: str | None = None, api_key: str | None = None,
                 timeout: float | None = None, connector: Callable | None = None) -> None:
        self.url = url or settings.bailian_mcp_web_search_url
        self.api_key = api_key if api_key is not None else settings.dashscope_api_key
        self.timeout = timeout if timeout is not None else settings.bailian_mcp_timeout_seconds
        self._connector = connector
        self._tools: tuple[float, list[McpTool]] | None = None
        self._lock = threading.Lock()

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        if not self.api_key:
            raise BailianMcpError("mcp_api_key_missing", "未配置百炼 API Key，联网搜索不可用。")
        if self._connector is not None:
            async with self._connector(self.url, {"Authorization": f"Bearer {self.api_key}"}) as session:
                await asyncio.wait_for(session.initialize(), timeout=self.timeout)
                yield session
            return
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:
            raise BailianMcpError("mcp_dependency_missing", "缺少 MCP SDK，联网搜索暂不可用。") from exc
        try:
            async with streamablehttp_client(
                self.url, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=self.timeout,
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), timeout=self.timeout)
                    yield session
        except BailianMcpError:
            raise
        except TimeoutError as exc:
            raise BailianMcpError("mcp_timeout", "联网搜索服务响应超时。") from exc
        except Exception as exc:
            logger.warning("百炼 MCP 连接失败：%s", type(exc).__name__)
            raise BailianMcpError("mcp_connection_failed", "联网搜索服务暂不可用。") from exc

    @staticmethod
    def _normalise_tools(raw_tools: list[Any]) -> list[McpTool]:
        tools: list[McpTool] = []
        for tool in raw_tools:
            name = getattr(tool, "name", None) if not isinstance(tool, dict) else tool.get("name")
            description = getattr(tool, "description", "") if not isinstance(tool, dict) else tool.get("description", "")
            schema = getattr(tool, "inputSchema", None) if not isinstance(tool, dict) else tool.get("inputSchema")
            if isinstance(name, str) and isinstance(schema, dict):
                tools.append(McpTool(name=name, description=description or "", input_schema=schema))
        return tools

    async def _list_tools_async(self) -> list[McpTool]:
        async with self._session() as session:
            result = await asyncio.wait_for(session.list_tools(), timeout=self.timeout)
            return self._normalise_tools(list(result.tools))

    def list_tools(self) -> list[McpTool]:
        with self._lock:
            if self._tools and time.monotonic() - self._tools[0] < _CACHE_SECONDS:
                return list(self._tools[1])
            tools = asyncio.run(self._list_tools_async())
            self._tools = (time.monotonic(), tools)
            return list(tools)

    @staticmethod
    def _query_field(tool: McpTool) -> str | None:
        properties = tool.input_schema.get("properties", {})
        for field in ("query", "search_query", "q", "keyword", "keywords"):
            if field in properties:
                return field
        return None

    def select_search_tool(self) -> tuple[McpTool, str]:
        tools = self.list_tools()
        configured = settings.bailian_mcp_web_search_tool.strip()
        if configured:
            tool = next((item for item in tools if item.name == configured), None)
            field = self._query_field(tool) if tool else None
            if not tool or not field:
                raise BailianMcpError("mcp_tool_unavailable", "配置的百炼搜索工具不存在或参数不兼容。")
            return tool, field
        candidates = [(tool, self._query_field(tool)) for tool in tools]
        candidates = [(tool, field) for tool, field in candidates
                      if field and ("search" in tool.name.casefold() or "搜索" in tool.description)]
        if len(candidates) != 1:
            raise BailianMcpError("mcp_tool_ambiguous", "无法唯一识别联网搜索工具，请在配置中指定工具名。")
        return candidates[0]

    @staticmethod
    def _text_content(result: Any) -> str:
        chunks = []
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None) if not isinstance(block, dict) else block.get("text")
            if isinstance(text, str):
                chunks.append(text)
        return "\n".join(chunks)

    async def _call_async(self, tool: McpTool, field: str, query: str) -> str:
        async with self._session() as session:
            result = await asyncio.wait_for(session.call_tool(tool.name, {field: query}), timeout=self.timeout)
            if getattr(result, "isError", False):
                raise BailianMcpError("mcp_tool_failed", "百炼联网搜索未成功返回结果。")
            return self._text_content(result)

    def search(self, query: str) -> tuple[McpTool, str]:
        tool, field = self.select_search_tool()
        return tool, asyncio.run(self._call_async(tool, field, query))
