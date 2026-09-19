"""本地确定性能力：读取服务器时钟并转换到指定时区。"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

from app.core.agent.models import ToolContext, ToolError, ToolResult


class DatetimeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    timezone: str | None = Field(default=None, max_length=100)


def get_current_datetime(args: DatetimeArgs, ctx: ToolContext, clock=None) -> ToolResult:
    zone = args.timezone if args.timezone is not None else ctx.default_timezone
    try:
        tz = ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        return ToolResult(status="needs_input", error=ToolError(
            code="invalid_timezone", message="无法识别时区，请提供有效的 IANA 时区，例如 Asia/Shanghai。"))
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if now.tzinfo is None:
        raise ValueError("clock must return an aware datetime")
    local = now.astimezone(tz)
    return ToolResult(status="ok", data={
        "datetime": local.isoformat(), "date": local.date().isoformat(),
        "weekday": "星期" + "一二三四五六日"[local.weekday()], "timezone": zone,
    }, provenance={"provider": "server_clock", "fetched_at": now.isoformat(),
                   "valid_at": local.isoformat()})
