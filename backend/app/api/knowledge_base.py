"""知识库 CRUD（Phase 2-02）+ 重新索引（Phase 3-05）。

- kb_default 为系统默认库（种子创建，禁止删除）
- documents.kb_id 无数据库外键（既有表无迁移框架），应用层操作前校验 + 显式级联
- 删除顺序固定（文档 02）：SQLite 记录 → Chroma collection → uploads 目录；
  SQLite 是唯一事务面，后两步失败记录日志（半删状态可由 collection/目录名重试清理）
- reindex（Phase 3-05）：双 buffer 重建，重复触发 409；详情见 app/core/reindex.py
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api.errors import BadRequestError, ConflictError, NotFoundError
from app.auth.dependencies import get_optional_current_user
from app.authorization.permissions import can_create_kb, list_operable_kbs, list_readable_kbs, require_kb_permission
from app.config import settings
from app.core.reindex import reindex_manager
from app.models.database import (
    ChunkRecord,
    Document,
    DocumentTopic,
    KnowledgeBase,
    KnowledgeBaseRolePermission,
    KnowledgeBaseUserPermission,
    Role,
    User,
)
from app.models.schemas import (
    DocumentOut,
    KnowledgeBaseCreate,
    KnowledgeBaseDetailOut,
    KnowledgeBaseOut,
    KnowledgeBasePermissionGrant,
    ReindexRequest,
    ReindexStatusOut,
)
from app.store.db import get_db
from app.store.vector_store import vector_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["knowledge-bases"])

KB_DEFAULT = "kb_default"


def _new_kb_id() -> str:
    return f"kb_{uuid4().hex[:12]}"


def _kb_out(db: Session, kb: KnowledgeBase) -> KnowledgeBaseOut:
    """列表/详情通用装配：附带文档与 chunk 统计（GROUP BY 一次查询带出）。"""
    rows = db.execute(
        select(Document.kb_id, func.count(Document.id), func.coalesce(func.sum(Document.chunk_count), 0))
        .group_by(Document.kb_id)
    ).all()
    stats = {r[0]: (int(r[1]), int(r[2])) for r in rows}
    doc_count, chunk_count = stats.get(kb.id, (0, 0))
    return KnowledgeBaseOut(
        id=kb.id,
        name=kb.name,
        description=kb.description,
        embedding_model=kb.embedding_model,
        access_level=kb.access_level,
        document_count=doc_count,
        chunk_count=chunk_count,
        created_at=kb.created_at,
    )


@router.post("/knowledge-bases", response_model=KnowledgeBaseOut, status_code=201)
def create_knowledge_base(
    body: KnowledgeBaseCreate,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_current_user),
) -> KnowledgeBaseOut:
    if db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == body.name)):
        raise ConflictError("duplicate_name", f"知识库名称已存在：{body.name}")
    if body.embedding_model is not None and body.embedding_model != settings.embedding_model:
        # 每库独立 embedding 模型后置（Phase 3-06 混合检索时评估）；先统一全局模型
        raise BadRequestError(
            "unsupported_embedding_model",
            f"当前仅支持全局 embedding 模型：{settings.embedding_model}",
        )
    if not can_create_kb(user, body.access_level):
        raise BadRequestError("insufficient_role_level", "只能创建低于自己角色等级的知识库")
    kb = KnowledgeBase(
        id=_new_kb_id(),
        name=body.name,
        description=body.description,
        access_level=body.access_level,
        # 旧字段同步一个粗略映射，兼容尚未升级的外部消费者。
        visibility={"guest": "public", "student": "authenticated", "editor": "restricted", "admin": "restricted"}[body.access_level],
        owner_id=user.id if user else None,
        embedding_model=body.embedding_model or settings.embedding_model,
    )
    db.add(kb)
    db.commit()
    db.refresh(kb)
    return _kb_out(db, kb)


@router.get("/knowledge-bases", response_model=list[KnowledgeBaseOut])
def list_knowledge_bases(
    db: Session = Depends(get_db), user: User | None = Depends(get_optional_current_user)
) -> list[KnowledgeBaseOut]:
    # 管理页只返回可操作的库；聊天端不会再调用此接口来选择知识库。
    return [_kb_out(db, kb) for kb in list_operable_kbs(db, user)]


@router.get("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseDetailOut)
def get_knowledge_base(
    kb_id: str, db: Session = Depends(get_db), user: User | None = Depends(get_optional_current_user)
) -> KnowledgeBaseDetailOut:
    kb = require_kb_permission(db, kb_id, user, "read")
    out = _kb_out(db, kb)
    docs = db.scalars(
        select(Document).where(Document.kb_id == kb_id).order_by(Document.created_at.desc())
    )
    docs = list(docs)
    topic_rows = db.execute(
        select(
            DocumentTopic.doc_id, DocumentTopic.topic_code, DocumentTopic.source,
            DocumentTopic.confidence, DocumentTopic.review_status,
        ).where(
            DocumentTopic.doc_id.in_([doc.id for doc in docs])
        )
    ).all() if docs else []
    topics: dict[str, dict[str, list]] = {doc.id: {"approved": [], "suggestions": []} for doc in docs}
    for doc_id, topic_code, source, confidence, review_status in topic_rows:
        entry = topics.setdefault(doc_id, {"approved": [], "suggestions": []})
        if review_status == "approved":
            entry["approved"].append(topic_code)
        else:
            entry["suggestions"].append({
                "topic_code": topic_code, "source": source,
                "confidence": confidence, "review_status": review_status,
            })
    return KnowledgeBaseDetailOut(
        **out.model_dump(),
        documents=[
            DocumentOut(
                doc_id=d.id, filename=d.filename, file_size=d.file_size,
                status=d.status, error_message=d.error_message,
                chunk_count=d.chunk_count, created_at=d.created_at,
                topics=topics.get(d.id, {}).get("approved", []),
                topic_suggestions=topics.get(d.id, {}).get("suggestions", []),
            )
            for d in docs
        ],
    )


@router.delete("/knowledge-bases/{kb_id}")
def delete_knowledge_base(
    kb_id: str, db: Session = Depends(get_db), user: User | None = Depends(get_optional_current_user)
) -> dict:
    """删除知识库：①SQLite 记录（事务面）→ ②Chroma collection → ③uploads 目录。

    documents.kb_id 无数据库外键（既有表无迁移框架），级联由本层显式执行：
    chunks → documents → knowledge_bases 同一事务删除。
    """
    kb = require_kb_permission(db, kb_id, user, "manage")
    if kb_id == KB_DEFAULT:
        raise BadRequestError("default_kb_protected", "默认知识库不可删除")

    file_paths = [
        d.file_path
        for d in db.scalars(select(Document).where(Document.kb_id == kb_id))
    ]
    # ① SQLite：显式级联（chunks → documents → kb 同一事务）
    db.execute(delete(ChunkRecord).where(ChunkRecord.kb_id == kb_id))
    db.execute(
        delete(DocumentTopic).where(
            DocumentTopic.doc_id.in_(select(Document.id).where(Document.kb_id == kb_id))
        )
    )
    db.execute(delete(Document).where(Document.kb_id == kb_id))
    db.delete(kb)
    db.commit()

    # ② Chroma collection（幂等，缺 collection 吞异常）
    vector_store.delete_collection(kb_id)

    # ③ 原始文件：逐文件删（兼容 Phase 1 平铺遗留）+ 删除 kb 子目录
    for p in file_paths:
        Path(p).unlink(missing_ok=True)
    shutil.rmtree(settings.uploads_dir / kb_id, ignore_errors=True)

    # ④ 关键词索引同步（缓存，异常不阻断）
    try:
        from app.core.keyword_index import keyword_index

        keyword_index.remove_kb(kb_id)
    except Exception:
        logger.exception("关键词索引同步失败（kb=%s）", kb_id)

    return {"deleted": kb_id}


@router.post("/knowledge-bases/{kb_id}/permissions")
def grant_knowledge_base_permission(
    kb_id: str,
    body: KnowledgeBasePermissionGrant,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_current_user),
) -> dict:
    """授予角色或单个用户权限；两者必须且只能提供一个。"""
    require_kb_permission(db, kb_id, user, "manage")
    if bool(body.role_code) == bool(body.user_id):
        raise BadRequestError("invalid_permission_subject", "必须指定角色或用户中的一个")
    if body.role_code:
        role = db.scalar(select(Role).where(Role.code == body.role_code))
        if role is None:
            raise NotFoundError("role_not_found", "角色不存在")
        exists = db.scalar(select(KnowledgeBaseRolePermission).where(
            KnowledgeBaseRolePermission.kb_id == kb_id,
            KnowledgeBaseRolePermission.role_id == role.id,
            KnowledgeBaseRolePermission.permission == body.permission,
        ))
        if exists is None:
            db.add(KnowledgeBaseRolePermission(kb_id=kb_id, role_id=role.id, permission=body.permission))
            db.commit()
        return {"subject_type": "role", "subject": role.code, "permission": body.permission}
    target = db.get(User, body.user_id)
    if target is None:
        raise NotFoundError("user_not_found", "用户不存在")
    exists = db.scalar(select(KnowledgeBaseUserPermission).where(
        KnowledgeBaseUserPermission.kb_id == kb_id,
        KnowledgeBaseUserPermission.user_id == target.id,
        KnowledgeBaseUserPermission.permission == body.permission,
    ))
    if exists is None:
        db.add(KnowledgeBaseUserPermission(kb_id=kb_id, user_id=target.id, permission=body.permission))
        db.commit()
    return {"subject_type": "user", "subject": target.id, "permission": body.permission}


@router.get("/knowledge-bases/{kb_id}/permissions")
def list_knowledge_base_permissions(
    kb_id: str,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_current_user),
) -> dict:
    """查看授权清单：仅知识库管理员可见，避免泄露成员信息。"""
    require_kb_permission(db, kb_id, user, "manage")
    role_rows = db.execute(
        select(KnowledgeBaseRolePermission, Role.code, Role.name)
        .join(Role, Role.id == KnowledgeBaseRolePermission.role_id)
        .where(KnowledgeBaseRolePermission.kb_id == kb_id)
        .order_by(Role.code, KnowledgeBaseRolePermission.permission)
    ).all()
    user_rows = db.execute(
        select(KnowledgeBaseUserPermission, User.id, User.username, User.nickname)
        .join(User, User.id == KnowledgeBaseUserPermission.user_id)
        .where(KnowledgeBaseUserPermission.kb_id == kb_id)
        .order_by(User.username, KnowledgeBaseUserPermission.permission)
    ).all()
    return {
        "role_permissions": [
            {
                "id": permission.id,
                "role_code": role_code,
                "role_name": role_name,
                "permission": permission.permission,
            }
            for permission, role_code, role_name in role_rows
        ],
        "user_permissions": [
            {
                "id": permission.id,
                "user_id": user_id,
                "username": username,
                "nickname": nickname,
                "permission": permission.permission,
            }
            for permission, user_id, username, nickname in user_rows
        ],
    }


@router.delete("/knowledge-bases/{kb_id}/role-permissions/{permission_id}")
def revoke_role_permission(
    kb_id: str,
    permission_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_current_user),
) -> dict:
    require_kb_permission(db, kb_id, user, "manage")
    row = db.get(KnowledgeBaseRolePermission, permission_id)
    if row is None or row.kb_id != kb_id:
        raise NotFoundError("permission_not_found", "角色授权记录不存在")
    db.delete(row)
    db.commit()
    return {"deleted": permission_id}


@router.delete("/knowledge-bases/{kb_id}/user-permissions/{permission_id}")
def revoke_user_permission(
    kb_id: str,
    permission_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_current_user),
) -> dict:
    require_kb_permission(db, kb_id, user, "manage")
    row = db.get(KnowledgeBaseUserPermission, permission_id)
    if row is None or row.kb_id != kb_id:
        raise NotFoundError("permission_not_found", "用户授权记录不存在")
    db.delete(row)
    db.commit()
    return {"deleted": permission_id}


# ===== 重新索引（Phase 3-05）=====

def _status_out(task) -> ReindexStatusOut:
    return ReindexStatusOut(
        kb_id=task.kb_id,
        doc_id=task.doc_id,
        status=task.status,
        total=task.total,
        done=task.done,
        current_doc=task.current_doc,
        docs_before=task.docs_before,
        docs_after=task.docs_after,
        error_message=task.error_message,
        started_at=task.started_at.isoformat() if task.started_at else None,
        finished_at=task.finished_at.isoformat() if task.finished_at else None,
    )


@router.post("/knowledge-bases/{kb_id}/reindex", response_model=ReindexStatusOut, status_code=202)
def reindex_knowledge_base(
    kb_id: str,
    background_tasks: BackgroundTasks,
    body: ReindexRequest | None = None,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_current_user),
) -> ReindexStatusOut:
    """重建索引：单文档（body.doc_id）或全库（缺省）。重建期间检索不中断（双 buffer）。

    重复触发（同一 kb 任务运行中）→ 409 reindex_in_progress。
    """
    require_kb_permission(db, kb_id, user, "write")
    if reindex_manager.is_running(kb_id):
        raise ConflictError("reindex_in_progress", "该知识库正在重建索引，请稍后再试")
    task = reindex_manager.start(kb_id, body.doc_id if body else None)
    background_tasks.add_task(reindex_manager.run, kb_id, task.doc_id)
    return _status_out(task)


@router.get("/knowledge-bases/{kb_id}/reindex/status", response_model=ReindexStatusOut)
def reindex_status(
    kb_id: str, db: Session = Depends(get_db), user: User | None = Depends(get_optional_current_user)
) -> ReindexStatusOut:
    """重建进度（无任务 → status=idle）。"""
    require_kb_permission(db, kb_id, user, "read")
    task = reindex_manager.get(kb_id)
    if task is None:
        return ReindexStatusOut(kb_id=kb_id, status="idle")
    return _status_out(task)
