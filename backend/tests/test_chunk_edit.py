from __future__ import annotations

from datetime import timedelta

import pytest

from app.api.errors import ApiError, ConflictError
from app.api.knowledge import documents
from app.models.database import (
    AuditLog,
    ChunkRecord,
    Document,
    DocumentVersion,
    DocumentVersionChunk,
    KnowledgeBase,
    Role,
    User,
)
from app.models.schemas import ChunkUpdate


def _seed(db_session):
    role = Role(id="role_editor", code="editor", name="编辑")
    user = User(
        id="user_editor", username="editor", password_hash="unused", nickname="编辑",
        roles=[role],
    )
    kb = KnowledgeBase(id="kb_chunks", name="Chunk 测试库", access_level="guest")
    doc = Document(
        id="doc_chunks", kb_id=kb.id, filename="guide.md", file_hash="hash",
        file_size=10, file_path="/tmp/guide.md", status="ready",
        governance_status="published", current_version_id="ver_chunks",
        published_version_id="ver_chunks", chunk_count=2,
    )
    version = DocumentVersion(
        id="ver_chunks", document_id=doc.id, version_no=1, file_path=doc.file_path,
        file_hash="hash", file_size=10, processing_status="ready", review_status="published",
    )
    live = [
        ChunkRecord(
            id=f"{doc.id}_{index}", doc_id=doc.id, document_version_id=version.id,
            kb_id=kb.id, chunk_index=index, text=text, char_length=len(text),
            token_estimate=1,
        )
        for index, text in enumerate(("旧内容", "其他内容"))
    ]
    staged = [
        DocumentVersionChunk(
            id=f"{version.id}_{index}", document_version_id=version.id, doc_id=doc.id,
            kb_id=kb.id, chunk_index=index, text=row.text,
            char_length=row.char_length, token_estimate=row.token_estimate,
        )
        for index, row in enumerate(live)
    ]
    db_session.add_all([role, user, kb, doc, version, *live, *staged])
    db_session.commit()
    return user, doc, live[0]


def test_update_chunk_syncs_sql_version_vector_keyword_and_audit(db_session, monkeypatch):
    user, doc, row = _seed(db_session)
    vector_calls = []
    keyword_calls = []
    monkeypatch.setattr(documents, "embed_texts", lambda texts: [[0.1], [0.2]])
    monkeypatch.setattr(
        documents.vector_store,
        "upsert_chunk",
        lambda kb_id, chunk, embedding: vector_calls.append((kb_id, chunk.text, embedding)),
    )
    from app.core.retrieval.indexing.keyword_index import keyword_index

    monkeypatch.setattr(
        keyword_index,
        "add_document",
        lambda kb_id, doc_id, chunks: keyword_calls.append(
            (kb_id, doc_id, [chunk.text for chunk in chunks])
        ),
    )

    result = documents.update_chunk(
        doc.id,
        0,
        ChunkUpdate(text="  校订后的内容  ", expected_updated_at=row.updated_at),
        db_session,
        user,
    )

    db_session.expire_all()
    saved = db_session.get(ChunkRecord, row.id)
    version_saved = db_session.get(DocumentVersionChunk, "ver_chunks_0")
    version = db_session.get(DocumentVersion, "ver_chunks")
    assert result["text"] == "校订后的内容"
    assert saved.text == "校订后的内容"
    assert saved.char_length == len("校订后的内容")
    assert version_saved.text == "校订后的内容"
    assert version.content_hash is not None
    assert version.change_summary == "人工校订 chunk #0"
    assert vector_calls == [("kb_chunks", "校订后的内容", [0.2])]
    assert keyword_calls[-1] == ("kb_chunks", "doc_chunks", ["校订后的内容", "其他内容"])
    assert db_session.query(AuditLog).filter_by(action="document.chunk_update").count() == 1


def test_update_chunk_rejects_stale_timestamp_before_indexing(db_session, monkeypatch):
    user, doc, row = _seed(db_session)
    monkeypatch.setattr(
        documents,
        "embed_texts",
        lambda texts: pytest.fail("并发冲突时不应调用向量服务"),
    )

    with pytest.raises(ConflictError) as error:
        documents.update_chunk(
            doc.id,
            0,
            ChunkUpdate(text="新内容", expected_updated_at=row.updated_at - timedelta(seconds=1)),
            db_session,
            user,
        )

    assert error.value.code == "chunk_modified"
    assert db_session.get(ChunkRecord, row.id).text == "旧内容"


def test_update_chunk_restores_vector_when_keyword_update_fails(db_session, monkeypatch):
    user, doc, row = _seed(db_session)
    vector_texts = []
    keyword_attempts = 0
    monkeypatch.setattr(documents, "embed_texts", lambda texts: [[0.1], [0.2]])
    monkeypatch.setattr(
        documents.vector_store,
        "upsert_chunk",
        lambda kb_id, chunk, embedding: vector_texts.append(chunk.text),
    )
    from app.core.retrieval.indexing.keyword_index import keyword_index

    def flaky_keyword_update(kb_id, doc_id, chunks):
        nonlocal keyword_attempts
        keyword_attempts += 1
        if keyword_attempts == 1:
            raise RuntimeError("keyword unavailable")

    monkeypatch.setattr(keyword_index, "add_document", flaky_keyword_update)

    with pytest.raises(ApiError) as error:
        documents.update_chunk(
            doc.id,
            0,
            ChunkUpdate(text="新内容", expected_updated_at=row.updated_at),
            db_session,
            user,
        )

    assert error.value.code == "chunk_index_update_failed"
    assert vector_texts == ["新内容", "旧内容"]
    assert keyword_attempts == 2
    assert db_session.get(ChunkRecord, row.id).text == "旧内容"
