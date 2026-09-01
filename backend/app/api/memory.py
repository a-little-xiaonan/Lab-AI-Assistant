"""用户自己的长期记忆管理接口（Phase 4-02）。"""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import NotFoundError
from app.auth.dependencies import get_current_user
from app.memory.long_term import long_term_memory
from app.models.database import User, UserMemory
from app.models.schemas import UserMemoryOut, UserMemoryUpdate
from app.store.db import get_db

router = APIRouter(tags=["memory"])


def _out(row: UserMemory) -> UserMemoryOut:
    return UserMemoryOut(
        id=row.id,
        memory_type=row.memory_type,
        content=row.content,
        confidence=row.confidence,
        source_session_id=row.source_session_id,
        scope_kb_id=row.scope_kb_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/users/me/memories", response_model=list[UserMemoryOut])
def list_my_memories(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[UserMemoryOut]:
    rows = db.scalars(
        select(UserMemory)
        .where(UserMemory.user_id == user.id, UserMemory.status == "active")
        .order_by(UserMemory.updated_at.desc())
    )
    return [_out(row) for row in rows]


@router.put("/users/me/memories/{memory_id}", response_model=UserMemoryOut)
def update_my_memory(
    memory_id: str,
    body: UserMemoryUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> UserMemoryOut:
    row = db.scalar(select(UserMemory).where(UserMemory.id == memory_id, UserMemory.user_id == user.id))
    if row is None or row.status != "active":
        raise NotFoundError("memory_not_found", "记忆不存在")
    row.content = body.content.strip()
    row.content_hash = hashlib.sha1(row.content.encode("utf-8")).hexdigest()
    db.commit()
    db.refresh(row)
    long_term_memory.upsert_memory_vector(row)
    return _out(row)


@router.delete("/users/me/memories/{memory_id}")
def delete_my_memory(
    memory_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    row = db.scalar(select(UserMemory).where(UserMemory.id == memory_id, UserMemory.user_id == user.id))
    if row is None or row.status != "active":
        raise NotFoundError("memory_not_found", "记忆不存在")
    row.status = "deleted"
    db.commit()
    long_term_memory.delete_memory_vector(row.id)
    return {"deleted": memory_id}


@router.delete("/users/me/memories")
def clear_my_memories(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    rows = db.scalars(
        select(UserMemory).where(UserMemory.user_id == user.id, UserMemory.status == "active")
    ).all()
    for row in rows:
        row.status = "deleted"
    db.commit()
    long_term_memory.clear_user_vectors(user.id)
    return {"deleted": len(rows)}
