from __future__ import annotations

from app.config import settings
from app.tools.core.clarification import ClarificationArgs, request_clarification
from app.tools.core.platform import ToolDefinition, ToolPlatform, ToolPolicy
from app.tools.knowledge.document import DocumentReadArgs, read_document_section
from app.tools.knowledge.search import KnowledgeArgs, search_knowledge
from app.tools.local.calculation import CalculateArgs, DateCalculateArgs, calculate, date_calculate
from app.tools.local.datetime import DatetimeArgs, get_current_datetime
from app.tools.web.search import WebReadArgs, WebSearchArgs, read_webpage, web_search


platform = ToolPlatform([
    ToolDefinition("get_current_datetime", "查询真实当前日期、星期和时间，默认上海时区。",
                   DatetimeArgs, get_current_datetime, ToolPolicy("public", "local")),
    ToolDefinition("search_knowledge", "查询用户有权读取的内部知识资料；输入补齐指代后的完整问题。",
                   KnowledgeArgs, search_knowledge, ToolPolicy("internal", "local")),
    ToolDefinition("read_document_section", "读取本次已检索到的、已发布文档相邻分块；不能读取任意文件。",
                   DocumentReadArgs, read_document_section,
                   ToolPolicy("internal", "local", enabled=lambda: settings.document_read_enabled)),
    ToolDefinition("calculate", "使用确定性十进制执行加减乘除、求和、均值或百分比变化；不执行表达式。",
                   CalculateArgs, calculate,
                   ToolPolicy("public", "local", enabled=lambda: settings.calculator_enabled)),
    ToolDefinition("date_calculate", "计算日期差、日期偏移、截止日比较或精确时长。",
                   DateCalculateArgs, date_calculate,
                   ToolPolicy("public", "local", enabled=lambda: settings.calculator_enabled)),
    ToolDefinition("web_search", "搜索公开互联网信息；不得查询内部知识或个人敏感资料。搜索结果只是线索，需用 read_webpage 核验后才能作为事实依据。",
                   WebSearchArgs, web_search,
                   ToolPolicy("public", "mcp", enabled=lambda: settings.web_search_enabled, cacheable=False)),
    ToolDefinition("read_webpage", "读取本次公开搜索结果，或用户本轮明确提供的公开链接；会限制地址、重定向、类型和内容大小。",
                   WebReadArgs, read_webpage,
                   ToolPolicy("public", "http", enabled=lambda: settings.web_read_enabled, cacheable=False)),
    ToolDefinition("request_clarification", "缺少关键参数或意图不明时追问；不访问外部服务。",
                   ClarificationArgs, request_clarification, ToolPolicy("public", "local")),
])

# 对已有调用与测试保留稳定 API；未来 MCP/HTTP 适配器也由 platform 注册。
TOOLS = platform.definitions


def schemas() -> list[dict]:
    return platform.schemas()


def execute(call: dict, ctx: ToolContext) -> ToolResult:
    return platform.execute(call, ctx)
