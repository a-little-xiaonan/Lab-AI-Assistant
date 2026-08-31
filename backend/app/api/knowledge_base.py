"""知识库路由：MVP 仅默认库 kb_default，多知识库 CRUD 在 Phase 2-02 实现。"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["knowledge-bases"])


@router.get("/knowledge-bases")
def list_knowledge_bases() -> list[dict]:
    return [{"id": "kb_default", "name": "默认知识库", "description": "MVP 单知识库，多库管理在 Phase 2"}]
