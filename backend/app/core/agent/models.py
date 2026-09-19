from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from app.core.agent.runtime import ExecutionBudget


class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class ToolResult(BaseModel):
    call_id: str = ""
    tool: str = ""
    status: Literal["ok", "no_data", "needs_input", "unsupported", "failed"]
    data: dict = Field(default_factory=dict)
    provenance: dict = Field(default_factory=dict)
    error: ToolError | None = None
    duration_ms: int = 0
    evidence_refs: list[str] = Field(default_factory=list)
    input_refs: list[str] = Field(default_factory=list)
    truncated: bool = False
    cache_hit: bool = False


@dataclass
class ToolContext:
    readable_kb_ids: tuple[str, ...]
    budget: ExecutionBudget
    default_timezone: str
    user_id: str | None = None
    anonymous_id: str | None = None
    session_id: str | None = None
    request_id: str = field(default_factory=lambda: uuid4().hex)
    current_query: str = ""
    history: str = ""
    calls: int = 0
    evidence_ledger: dict = field(default_factory=dict)
    resource_handles: dict = field(default_factory=dict)
    cache: dict = field(default_factory=dict)
