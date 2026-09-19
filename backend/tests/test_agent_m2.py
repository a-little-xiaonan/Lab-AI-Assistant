"""M2 工具平台、文档展开和确定性计算的离线验收。"""
from datetime import timedelta
from unittest.mock import patch

import pytest

from app.config import settings
from app.core.agent.models import ToolContext
from app.core.agent.runtime import ExecutionBudget
from app.models.database import ChunkRecord, Document, utcnow
from app.tools import registry
from app.tools.knowledge import document as document_tool
from app.core import rag_pipeline
from app.core.retrieval.retriever import RetrievedChunk


def ctx(session_id="sess_m2", kbs=("kb_default",)):
    import time
    return ToolContext(kbs, ExecutionBudget(time.monotonic() + 5), "Asia/Shanghai", session_id=session_id)


def call(name, arguments, call_id="call_m2"):
    import json
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def test_platform_schema_hides_disabled_tools(monkeypatch):
    monkeypatch.setattr(settings, "document_read_enabled", False)
    monkeypatch.setattr(settings, "calculator_enabled", False)
    names = {item["function"]["name"] for item in registry.schemas()}
    assert {"get_current_datetime", "search_knowledge", "request_clarification"} <= names
    assert "calculate" not in names and "read_document_section" not in names


def test_platform_declarations_have_policy():
    for name, definition in registry.TOOLS.items():
        assert definition.version == "v1"
        assert definition.policy.adapter in {"local", "mcp", "http"}
        assert definition.policy.data_classification in {"public", "internal", "sensitive"}
        assert definition.args.model_config["extra"] == "forbid"


@pytest.mark.parametrize(("arguments", "expected"), [
    ({"operation": "multiply", "operands": ["6", "12"], "unit": "小时"}, "72.0000"),
    ({"operation": "percent_change", "operands": ["80", "100"]}, "25.0000"),
    ({"operation": "mean", "operands": ["1", "2", "2"]}, "1.6667"),
])
def test_decimal_calculation(monkeypatch, arguments, expected):
    monkeypatch.setattr(settings, "calculator_enabled", True)
    result = registry.execute(call("calculate", arguments), ctx())
    assert result.status == "ok"
    assert result.data["result"] == expected


@pytest.mark.parametrize("arguments,code", [
    ({"operation": "divide", "operands": ["1", "0"]}, "division_by_zero"),
    ({"operation": "percent_change", "operands": ["0", "1"]}, "zero_base"),
    ({"operation": "add", "operands": ["1", "2", "3"]}, "operand_count"),
    ({"operation": "sum", "operands": ["NaN"]}, "invalid_arguments"),
])
def test_decimal_rejects_invalid(monkeypatch, arguments, code):
    monkeypatch.setattr(settings, "calculator_enabled", True)
    result = registry.execute(call("calculate", arguments), ctx())
    assert result.error.code == code


def test_calculation_input_reference(monkeypatch):
    monkeypatch.setattr(settings, "calculator_enabled", True)
    context = ctx()
    bad = registry.execute(call("calculate", {"operation": "add", "operands": ["1", "2"],
                                                "input_refs": ["evidence_unknown"]}), context)
    assert bad.error.code == "invalid_input_reference"
    context.evidence_ledger["evidence_1"] = {"data": {"value": "1"}}
    good = registry.execute(call("calculate", {"operation": "add", "operands": ["1", "2"],
                                                 "input_refs": ["evidence_1"]}, "call_ref"), context)
    assert good.input_refs == ["evidence_1"]


def test_date_calculation(monkeypatch):
    monkeypatch.setattr(settings, "calculator_enabled", True)
    result = registry.execute(call("date_calculate", {"operation": "days_between", "start": "2026-09-18",
                                                       "end": "2026-09-20"}), ctx())
    assert result.data["days"] == 2
    inclusive = registry.execute(call("date_calculate", {"operation": "days_between", "start": "2026-09-18",
                                                           "end": "2026-09-20", "inclusive": True}, "inclusive"), ctx())
    assert inclusive.data["days"] == 3
    offset = registry.execute(call("date_calculate", {"operation": "add_weeks", "start": "2026-09-18",
                                                        "amount": 2}, "offset"), ctx())
    assert offset.data["result"] == "2026-10-02"


def test_date_rejects_ambiguous_local_time(monkeypatch):
    monkeypatch.setattr(settings, "calculator_enabled", True)
    result = registry.execute(call("date_calculate", {"operation": "exact_duration", "timezone": "America/New_York",
                                                       "start": "2026-11-01T01:30:00", "end": "2026-11-01T02:30:00"}), ctx())
    assert result.error.code == "invalid_datetime"


def _document_fixture(db_session):
    now = utcnow()
    doc = Document(id="doc_m2", kb_id="kb_default", filename="rules.md", file_hash="hash", file_path="/not/read",
                   status="ready", governance_status="published", published_version_id="ver_m2",
                   current_version_id="ver_m2", effective_at=now - timedelta(days=1))
    db_session.add(doc)
    for index, text in enumerate(["报名要求是完成申请。", "例外：大一新生也可报名。", "截止日期以通知为准。"]):
        db_session.add(ChunkRecord(id=f"doc_m2_{index}", doc_id=doc.id, kb_id=doc.kb_id,
                                   document_version_id="ver_m2", chunk_index=index, text=text))
    db_session.commit()
    return doc


def _handle(context):
    context.resource_handles["docres_valid_handle"] = {"kind": "document_chunk", "session_id": context.session_id,
        "kb_id": "kb_default", "doc_id": "doc_m2", "document_version_id": "ver_m2", "chunk_index": 1,
        "chunk_id": "doc_m2_1"}


def test_document_read_requires_server_handle(monkeypatch, db_session):
    monkeypatch.setattr(settings, "document_read_enabled", True)
    _document_fixture(db_session)
    monkeypatch.setattr(document_tool, "SessionLocal", lambda: db_session)
    result = registry.execute(call("read_document_section", {"handle": "docres_forged", "question": "报名例外"}), ctx())
    assert result.error.code == "invalid_handle"


def test_document_read_and_recheck(monkeypatch, db_session):
    monkeypatch.setattr(settings, "document_read_enabled", True)
    _document_fixture(db_session)
    monkeypatch.setattr(document_tool, "SessionLocal", lambda: db_session)
    context = ctx()
    _handle(context)
    result = registry.execute(call("read_document_section", {"handle": "docres_valid_handle", "before": 1,
                                                              "after": 1, "question": "大一新生能报名吗？"}), context)
    assert result.status == "ok"
    assert len(result.data["chunks"]) >= 1
    assert "例外" in "".join(chunk["text"] for chunk in result.data["chunks"])
    assert result.data["document_version_id"] == "ver_m2"


@pytest.mark.parametrize("field,value", [("governance_status", "draft"), ("published_version_id", "ver_new")])
def test_document_change_or_withdrawal_rejected(monkeypatch, db_session, field, value):
    monkeypatch.setattr(settings, "document_read_enabled", True)
    doc = _document_fixture(db_session)
    setattr(doc, field, value)
    db_session.commit()
    monkeypatch.setattr(document_tool, "SessionLocal", lambda: db_session)
    context = ctx()
    _handle(context)
    result = registry.execute(call("read_document_section", {"handle": "docres_valid_handle", "question": "报名"}), context)
    assert result.status == "no_data"
    assert result.error.code == "resource_changed"


def test_document_handle_cannot_cross_session(monkeypatch, db_session):
    monkeypatch.setattr(settings, "document_read_enabled", True)
    _document_fixture(db_session)
    monkeypatch.setattr(document_tool, "SessionLocal", lambda: db_session)
    context = ctx("other")
    _handle(context)
    context.resource_handles["docres_valid_handle"]["session_id"] = "owner"
    result = registry.execute(call("read_document_section", {"handle": "docres_valid_handle", "question": "报名"}), context)
    assert result.status == "unsupported"
    assert result.error.code == "handle_not_authorized"


def test_weak_knowledge_candidate_only_issues_locator(monkeypatch):
    context = ctx()
    weak = RetrievedChunk("doc_weak_3", "不应直接展示的弱候选正文", .1,
                          {"doc_id": "doc_weak", "document_version_id": "ver_weak", "chunk_index": 3,
                           "knowledge_base_id": "kb_default", "source_file": "private.md"})
    decision = type("Decision", (), {"as_dict": lambda self: {"accepted": False}})()
    evidence = rag_pipeline.KnowledgeEvidence([], [weak], decision, status="no_data")
    monkeypatch.setattr(rag_pipeline, "prepare_evidence", lambda *args, **kwargs: evidence)
    result = registry.execute(call("search_knowledge", {"question": "例外条款"}), context)
    assert result.status == "no_data"
    assert "不应直接展示" not in result.model_dump_json()
    assert result.data["expandable_documents"][0]["document_handle"] in context.resource_handles
