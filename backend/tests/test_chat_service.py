"""P2 统一聊天服务：回合状态、历史快照、租约和依据原子性。"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.api.errors import BadRequestError, ConflictError
from app.models.database import AnswerProvenance, ChatSession, ChatTurn, Message, utcnow
from app.services.conversation.chat_service import ChatService
from app.services.conversation.history import build_history_snapshot


def _session(db, session_id="sess_p2"):
    session = ChatSession(id=session_id, knowledge_base_id="kb_default")
    db.add(session)
    db.commit()
    return session


def _service(db, session, query="问题", target_message_id=None):
    return ChatService(db, session, query, ["kb_default"], None, "guest", target_message_id)


def test_completed_turn_atomically_persists_provenance_and_revision(db_session):
    session = _session(db_session)
    service = _service(db_session, session)
    snapshot = service.start()
    assert snapshot.revision == 0 and snapshot.turns == ()

    assistant = service.complete({
        "answer": "回答", "sources": [{"source_file": "手册.pdf", "page": 2}],
        "_evidence_refs": [{"type": "knowledge", "knowledge_base_id": "kb_default",
                            "doc_id": "doc_1", "document_version_id": "ver_1",
                            "chunk_id": "chunk_1", "chunk_index": 0}],
        "_provenance": {"action": "answer", "answer_kind": "knowledge",
                        "basis_status": "recorded", "parts": []},
    })
    db_session.refresh(session)
    turn = db_session.get(ChatTurn, service.turn_id)
    provenance = db_session.get(AnswerProvenance, assistant.id)
    assert turn.status == "completed"
    assert provenance.answer_kind == "knowledge"
    assert '"chunk_id": "chunk_1"' in provenance.evidence_refs_json
    assert session.history_revision == 1
    assert session.active_turn_id is None and session.lease_expires_at is None


def test_failed_turn_is_excluded_from_history(db_session):
    session = _session(db_session)
    service = _service(db_session, session)
    service.start()
    service.fail("model_failed")

    snapshot = build_history_snapshot(db_session, session)
    messages = list(db_session.scalars(select(Message).where(Message.session_id == session.id)))
    assert [message.role for message in messages] == ["user"]
    assert db_session.get(ChatTurn, service.turn_id).status == "failed"
    assert snapshot.turns == () and snapshot.text == ""


def test_active_lease_rejects_second_turn_without_saving_user(db_session):
    session = _session(db_session)
    session.active_turn_id = "turn_existing"
    session.lease_expires_at = utcnow() + timedelta(minutes=1)
    db_session.add(ChatTurn(id="turn_existing", session_id=session.id, status="processing"))
    db_session.commit()

    service = _service(db_session, session, "第二个问题")
    with pytest.raises(ConflictError) as exc:
        service.start()
    assert exc.value.code == "session_busy"
    assert db_session.scalar(select(Message).where(Message.content == "第二个问题")) is None


def test_expired_lease_is_taken_over_and_old_turn_abandoned(db_session):
    session = _session(db_session)
    session.active_turn_id = "turn_old"
    session.lease_expires_at = utcnow() - timedelta(seconds=1)
    db_session.add(ChatTurn(id="turn_old", session_id=session.id, status="processing"))
    db_session.commit()

    service = _service(db_session, session)
    service.start()
    assert db_session.get(ChatTurn, "turn_old").status == "abandoned"
    service.cancel()


def test_target_message_must_be_assistant_in_same_session(db_session):
    session = _session(db_session)
    other = _session(db_session, "sess_other")
    message = Message(session_id=other.id, role="assistant", content="别的会话")
    db_session.add(message)
    db_session.commit()

    with pytest.raises(BadRequestError) as exc:
        _service(db_session, session, target_message_id=message.id).start()
    assert exc.value.code == "invalid_target_message"


def test_history_uses_completed_turns_and_keeps_pairs(db_session, monkeypatch):
    from app.config import settings

    session = _session(db_session)
    for index in range(3):
        turn = ChatTurn(
            id=f"turn_{index}", session_id=session.id, status="completed",
            history_revision=index, finished_at=utcnow(),
        )
        db_session.add(turn)
        db_session.flush()
        db_session.add_all([
            Message(session_id=session.id, turn_id=turn.id, role="user", content=f"问{index}"),
            Message(session_id=session.id, turn_id=turn.id, role="assistant", content=f"答{index}"),
        ])
    failed = ChatTurn(id="turn_failed", session_id=session.id, status="failed")
    db_session.add(failed)
    db_session.add(Message(session_id=session.id, turn_id=failed.id, role="user", content="失败问题"))
    session.history_revision = 3
    db_session.commit()
    monkeypatch.setattr(settings, "history_max_tokens", 5)

    snapshot = build_history_snapshot(db_session, session)
    assert "失败问题" not in snapshot.text
    assert all(f"user: 问{i}" in snapshot.text if f"assistant: 答{i}" in snapshot.text else True
               for i in range(3))
    assert len(snapshot.message_ids) % 2 == 0


def test_legacy_message_pairs_are_backfilled_without_claiming_orphans(
    db_session, monkeypatch
):
    from app.store import db as db_module
    session = _session(db_session, "sess_legacy")
    db_session.add_all([
        Message(session_id=session.id, role="user", content="旧问题"),
        Message(session_id=session.id, role="assistant", content="旧回答"),
        Message(session_id=session.id, role="user", content="失败遗留问题"),
    ])
    db_session.commit()
    test_factory = sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_module, "SessionLocal", test_factory)

    db_module._backfill_chat_turns()
    db_session.expire_all()
    messages = list(db_session.scalars(
        select(Message).where(Message.session_id == session.id).order_by(Message.id)
    ))
    assert messages[0].turn_id == messages[1].turn_id
    assert messages[2].turn_id is None
    assert db_session.get(ChatTurn, messages[0].turn_id).status == "completed"
    assert db_session.get(ChatSession, session.id).history_revision == 1
