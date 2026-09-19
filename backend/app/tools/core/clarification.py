"""不访问数据源的澄清控制工具。"""
from pydantic import BaseModel, ConfigDict, Field

from app.core.agent.models import ToolContext, ToolResult


class ClarificationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, max_length=300)


def request_clarification(args: ClarificationArgs, ctx: ToolContext) -> ToolResult:
    return ToolResult(status="needs_input", data={"question": args.question})
