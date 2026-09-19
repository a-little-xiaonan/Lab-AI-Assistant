"""本地确定性能力：不解释表达式，只执行 Decimal 与日期运算。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.agent.models import ToolContext, ToolError, ToolResult

MAX_DECIMAL_CHARS = 64


class CalculateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal["add", "subtract", "multiply", "divide", "sum", "mean", "percent_change"]
    operands: list[str] = Field(min_length=1, max_length=20)
    unit: str | None = Field(default=None, max_length=40)
    decimal_places: int = Field(default=4, ge=0, le=12)
    input_refs: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("operands")
    @classmethod
    def validate_operands(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > MAX_DECIMAL_CHARS for value in values):
            raise ValueError("invalid decimal")
        try:
            parsed = [Decimal(value) for value in values]
        except InvalidOperation as exc:
            raise ValueError("invalid decimal") from exc
        if any(not value.is_finite() for value in parsed):
            raise ValueError("non-finite decimal")
        return values


class DateCalculateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal["days_between", "add_days", "add_weeks", "deadline_status", "exact_duration"]
    start: str | None = Field(default=None, max_length=64)
    end: str | None = Field(default=None, max_length=64)
    amount: int | None = Field(default=None, ge=-3650, le=3650)
    timezone: str | None = Field(default=None, max_length=100)
    inclusive: bool = False
    input_refs: list[str] = Field(default_factory=list, max_length=20)


def _failure(code: str, message: str, status: str = "needs_input") -> ToolResult:
    return ToolResult(status=status, error=ToolError(code=code, message=message))


def calculate(args: CalculateArgs, ctx: ToolContext) -> ToolResult:
    if any(ref not in ctx.evidence_ledger for ref in args.input_refs):
        return _failure("invalid_input_reference", "计算引用的数据依据在本次请求中不存在。")
    values = [Decimal(value) for value in args.operands]
    operation = args.operation
    if operation in {"subtract", "divide", "percent_change"} and len(values) != 2:
        return _failure("operand_count", f"{operation} 需要恰好两个操作数。")
    if operation in {"add", "multiply"} and len(values) != 2:
        return _failure("operand_count", f"{operation} 需要恰好两个操作数。")
    if operation == "add": result, formula = values[0] + values[1], "a + b"
    elif operation == "subtract": result, formula = values[0] - values[1], "a - b"
    elif operation == "multiply": result, formula = values[0] * values[1], "a × b"
    elif operation == "divide":
        if values[1] == 0:
            return _failure("division_by_zero", "除数不能为零。")
        result, formula = values[0] / values[1], "a ÷ b"
    elif operation == "sum": result, formula = sum(values), "Σ operands"
    elif operation == "mean": result, formula = sum(values) / len(values), "Σ operands / count"
    else:
        if values[0] == 0:
            return _failure("zero_base", "百分比变化的基数不能为零。")
        result, formula = (values[1] - values[0]) / values[0] * Decimal("100"), "(new - old) / old × 100%"
    quantum = Decimal(1).scaleb(-args.decimal_places)
    rounded = result.quantize(quantum, rounding=ROUND_HALF_UP)
    unit = "%" if operation == "percent_change" else args.unit
    return ToolResult(status="ok", input_refs=args.input_refs, data={"operation": operation, "operands": args.operands,
        "formula": formula, "result": format(rounded, "f"), "unit": unit,
        "decimal_places": args.decimal_places}, provenance={"provider": "decimal_local"})


def _zone(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value or "")
    except ValueError:
        return None


def _datetime(value: str | None, zone: ZoneInfo) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value or "")
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(zone)
    # Local wall time that maps to two offsets or none is not selected silently.
    first, second = parsed.replace(tzinfo=zone, fold=0), parsed.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        return None
    return first


def date_calculate(args: DateCalculateArgs, ctx: ToolContext) -> ToolResult:
    if any(ref not in ctx.evidence_ledger for ref in args.input_refs):
        return _failure("invalid_input_reference", "计算引用的数据依据在本次请求中不存在。")
    zone_name = args.timezone or ctx.default_timezone
    zone = _zone(zone_name)
    if zone is None:
        return _failure("invalid_timezone", "请提供有效的 IANA 时区，例如 Asia/Shanghai。")
    if args.operation in {"days_between", "deadline_status"}:
        start, end = _date(args.start), _date(args.end)
        if not start or not end:
            return _failure("invalid_date", "此操作需要 YYYY-MM-DD 格式的起始和目标日期。")
        delta = (end - start).days
        if args.operation == "days_between":
            return ToolResult(status="ok", input_refs=args.input_refs, data={"operation": args.operation, "start": start.isoformat(),
                "end": end.isoformat(), "days": delta + (1 if args.inclusive else 0),
                "inclusive": args.inclusive, "timezone": zone_name,
                "counting": "包含两端" if args.inclusive else "不包含起始日"}, provenance={"provider": "date_local"})
        return ToolResult(status="ok", input_refs=args.input_refs, data={"operation": args.operation, "date": start.isoformat(),
            "deadline_date": end.isoformat(), "status": "before" if delta > 0 else "on_date" if delta == 0 else "after",
            "timezone": zone_name, "precision": "date_only"}, provenance={"provider": "date_local"})
    if args.operation in {"add_days", "add_weeks"}:
        start = _date(args.start)
        if not start or args.amount is None:
            return _failure("missing_date_or_amount", "此操作需要起始日期和偏移量。")
        amount = args.amount * (7 if args.operation == "add_weeks" else 1)
        result = start + timedelta(days=amount)
        return ToolResult(status="ok", input_refs=args.input_refs, data={"operation": args.operation, "start": start.isoformat(),
            "amount": args.amount, "result": result.isoformat(), "timezone": zone_name,
            "counting": "日历日"}, provenance={"provider": "date_local"})
    start, end = _datetime(args.start, zone), _datetime(args.end, zone)
    if not start or not end:
        return _failure("invalid_datetime", "精确时长需要带偏移的 ISO 日期时间；夏令时歧义时间请显式写出偏移。")
    seconds = int((end.astimezone(ZoneInfo("UTC")) - start.astimezone(ZoneInfo("UTC"))).total_seconds())
    return ToolResult(status="ok", input_refs=args.input_refs, data={"operation": "exact_duration", "start": start.isoformat(),
        "end": end.isoformat(), "seconds": seconds, "timezone": zone_name}, provenance={"provider": "date_local"})
