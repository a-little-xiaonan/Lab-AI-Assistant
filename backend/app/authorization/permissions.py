"""知识库 ACL：所有检索与管理操作在此收口，避免前端隐藏按钮成为唯一防线。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import ApiError, NotFoundError
from app.auth.dependencies import has_role
from app.models.database import (
    KnowledgeBase,
    KnowledgeBaseRolePermission,
    KnowledgeBaseUserPermission,
    User,
)

_LEVEL = {"read": 1, "write": 2, "manage": 3}


def _grants(permission: str, required: str) -> bool:
    return _LEVEL.get(permission, 0) >= _LEVEL[required]


def can_access_kb(db: Session, kb: KnowledgeBase, user: User | None, required: str) -> bool:
    """判断当前用户对单个活跃知识库的权限。"""
    if kb.status != "active":
        return False
    if user is not None and has_role(user, "admin"):
        return True
    if user is not None and kb.owner_id == user.id:
        return True
    if required == "read":
        if kb.visibility == "public":
            return True
        if kb.visibility == "authenticated" and user is not None:
            return True
    if user is None:
        return False

    direct = db.scalars(
        select(KnowledgeBaseUserPermission.permission).where(
            KnowledgeBaseUserPermission.kb_id == kb.id,
            KnowledgeBaseUserPermission.user_id == user.id,
        )
    )
    if any(_grants(p, required) for p in direct):
        return True
    role_ids = [role.id for role in user.roles]
    if not role_ids:
        return False
    inherited = db.scalars(
        select(KnowledgeBaseRolePermission.permission).where(
            KnowledgeBaseRolePermission.kb_id == kb.id,
            KnowledgeBaseRolePermission.role_id.in_(role_ids),
        )
    )
    return any(_grants(p, required) for p in inherited)


def require_kb_permission(
    db: Session, kb_id: str, user: User | None, required: str = "read"
) -> KnowledgeBase:
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None or kb.status != "active":
        raise NotFoundError("knowledge_base_not_found", "知识库不存在或不可用")
    if not can_access_kb(db, kb, user, required):
        if user is None and kb.visibility != "public":
            raise ApiError(401, "authentication_required", "请登录后访问该知识库")
        raise ApiError(403, "forbidden", "无权访问该知识库")
    return kb


def list_readable_kbs(db: Session, user: User | None) -> list[KnowledgeBase]:
    """知识库规模小，首期在应用层统一判定，避免 ACL SQL 过早复杂化。"""
    all_kbs = db.scalars(
        select(KnowledgeBase).where(KnowledgeBase.status == "active").order_by(KnowledgeBase.created_at)
    ).all()
    return [kb for kb in all_kbs if can_access_kb(db, kb, user, "read")]
