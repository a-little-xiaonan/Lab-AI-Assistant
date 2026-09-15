"""统一资源—操作权限策略：所有角色和知识库等级判断的唯一事实源。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import ApiError, NotFoundError
from app.auth.dependencies import get_current_user
from app.models.database import KnowledgeBase, User

ACCESS_LEVELS = ("guest", "student", "editor", "admin")
LEVEL_VALUE = {name: index for index, name in enumerate(ACCESS_LEVELS)}

PUBLIC_ACTIONS = {"chat.ask"}
AUTHENTICATED_ACTIONS = {"session.read_own", "session.manage_own", "memory.manage_own"}
CONTENT_ACTIONS = {
    "kb.create", "kb.manage", "document.read", "document.upload", "document.manage",
    "document.review_topic", "document.submit_review", "document.preapprove",
}
ADMIN_ACTIONS = {
    "document.publish", "document.archive", "document.rollback",
    "user.read", "user.manage", "audit.read",
}


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    reason_code: str
    actor_role: str
    resource_level: str | None = None


def effective_level(user: User | None) -> str:
    if user is None:
        return "guest"
    roles = {role.code for role in user.roles}
    return max((role for role in roles if role in LEVEL_VALUE), key=LEVEL_VALUE.get, default="guest")


def can_read_kb(kb: KnowledgeBase, user: User | None) -> bool:
    return kb.status == "active" and LEVEL_VALUE[effective_level(user)] >= LEVEL_VALUE.get(kb.access_level, 0)


def can_operate_kb(kb: KnowledgeBase, user: User | None) -> bool:
    level = effective_level(user)
    if level == "admin":
        return kb.status == "active"
    return kb.status == "active" and LEVEL_VALUE[level] > LEVEL_VALUE.get(kb.access_level, 0)


def can_create_kb(user: User | None, access_level: str) -> bool:
    level = effective_level(user)
    if access_level not in LEVEL_VALUE:
        return False
    return level == "admin" or LEVEL_VALUE[level] > LEVEL_VALUE[access_level]


def authorize(
    actor: User | None,
    action: str,
    resource: KnowledgeBase | None = None,
    *,
    resource_level: str | None = None,
) -> AuthorizationDecision:
    """返回明确决策，不在业务代码中散落角色字符串判断。"""
    role = effective_level(actor)
    target_level = resource.access_level if resource is not None else resource_level
    if actor is not None and (actor.status != "active" or actor.deleted_at is not None):
        return AuthorizationDecision(False, "account_unavailable", role, target_level)
    if action in PUBLIC_ACTIONS:
        return AuthorizationDecision(True, "allowed", role, target_level)
    if action in AUTHENTICATED_ACTIONS:
        return AuthorizationDecision(actor is not None, "allowed" if actor else "authentication_required", role)
    if action == "kb.read":
        allowed = resource is not None and can_read_kb(resource, actor)
        return AuthorizationDecision(allowed, "allowed" if allowed else "insufficient_role_level", role, target_level)
    if action == "kb.create":
        allowed = can_create_kb(actor, resource_level or "")
        return AuthorizationDecision(allowed, "allowed" if allowed else "insufficient_role_level", role, target_level)
    if action in CONTENT_ACTIONS:
        if actor is None or role not in {"editor", "admin"}:
            return AuthorizationDecision(False, "forbidden", role, target_level)
        if resource is None:
            return AuthorizationDecision(True, "allowed", role, target_level)
        allowed = can_operate_kb(resource, actor)
        return AuthorizationDecision(allowed, "allowed" if allowed else "insufficient_role_level", role, target_level)
    if action in ADMIN_ACTIONS:
        allowed = actor is not None and role == "admin"
        return AuthorizationDecision(allowed, "allowed" if allowed else "admin_required", role, target_level)
    return AuthorizationDecision(False, "unknown_action", role, target_level)


def enforce_action(
    actor: User | None,
    action: str,
    resource: KnowledgeBase | None = None,
    *,
    resource_level: str | None = None,
) -> AuthorizationDecision:
    decision = authorize(actor, action, resource, resource_level=resource_level)
    if decision.allowed:
        return decision
    if decision.reason_code == "authentication_required":
        raise ApiError(401, decision.reason_code, "请先登录")
    if decision.reason_code == "account_unavailable":
        raise ApiError(401, decision.reason_code, "账号不可用，请重新登录")
    raise ApiError(403, decision.reason_code, "当前账号没有执行此操作的权限")


def require_action(action: str) -> Callable[..., User]:
    """用于不依赖具体资源的路由级 action 校验。"""
    def _dependency(user: User = Depends(get_current_user)) -> User:
        enforce_action(user, action)
        return user
    return _dependency


def require_kb_permission(
    db: Session, kb_id: str, user: User | None, required: str = "read"
) -> KnowledgeBase:
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None or kb.status != "active":
        raise NotFoundError("knowledge_base_not_found", "知识库不存在或不可用")
    action = "kb.read" if required == "read" else "kb.manage"
    decision = authorize(user, action, kb)
    if decision.allowed:
        return kb
    if user is None and LEVEL_VALUE.get(kb.access_level, 0) > 0:
        raise ApiError(401, "authentication_required", "请登录后访问该等级知识库")
    raise ApiError(403, decision.reason_code, "当前角色等级不足，无法操作该知识库")


def list_readable_kbs(db: Session, user: User | None) -> list[KnowledgeBase]:
    all_kbs = db.scalars(
        select(KnowledgeBase).where(KnowledgeBase.status == "active").order_by(KnowledgeBase.created_at)
    ).all()
    return [kb for kb in all_kbs if authorize(user, "kb.read", kb).allowed]


def list_operable_kbs(db: Session, user: User | None) -> list[KnowledgeBase]:
    all_kbs = db.scalars(
        select(KnowledgeBase).where(KnowledgeBase.status == "active").order_by(KnowledgeBase.created_at)
    ).all()
    return [kb for kb in all_kbs if authorize(user, "kb.manage", kb).allowed]
