"""知识库等级授权：读取向下兼容，操作必须高一级。

guest 是匿名态（不入 roles 表）；登录用户的有效等级取其全部角色中的最高等级。
历史 visibility / ACL 表保留给旧数据和接口兼容，但不再作为本规则的授权来源。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import ApiError, NotFoundError
from app.models.database import KnowledgeBase, User

ACCESS_LEVELS = ("guest", "student", "editor", "admin")
LEVEL_VALUE = {name: index for index, name in enumerate(ACCESS_LEVELS)}


def effective_level(user: User | None) -> str:
    """获取身份的最高等级；多角色用户按最高权限生效。"""
    if user is None:
        return "guest"
    roles = {role.code for role in user.roles}
    return max((role for role in roles if role in LEVEL_VALUE), key=LEVEL_VALUE.get, default="guest")


def can_read_kb(kb: KnowledgeBase, user: User | None) -> bool:
    """读取规则：用户等级不低于知识库等级。"""
    return kb.status == "active" and LEVEL_VALUE[effective_level(user)] >= LEVEL_VALUE.get(kb.access_level, 0)


def can_operate_kb(kb: KnowledgeBase, user: User | None) -> bool:
    """操作规则：普通角色必须高于目标库；admin 是系统兜底，可维护 admin 库。"""
    level = effective_level(user)
    if level == "admin":
        return kb.status == "active"
    return kb.status == "active" and LEVEL_VALUE[level] > LEVEL_VALUE.get(kb.access_level, 0)


def can_create_kb(user: User | None, access_level: str) -> bool:
    """创建规则：editor 可创建 guest/student 库；admin 可创建全部等级。"""
    level = effective_level(user)
    if access_level not in LEVEL_VALUE:
        return False
    return level == "admin" or LEVEL_VALUE[level] > LEVEL_VALUE[access_level]


def require_kb_permission(
    db: Session, kb_id: str, user: User | None, required: str = "read"
) -> KnowledgeBase:
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None or kb.status != "active":
        raise NotFoundError("knowledge_base_not_found", "知识库不存在或不可用")
    allowed = can_read_kb(kb, user) if required == "read" else can_operate_kb(kb, user)
    if allowed:
        return kb
    if user is None and LEVEL_VALUE.get(kb.access_level, 0) > 0:
        raise ApiError(401, "authentication_required", "请登录后访问该等级知识库")
    raise ApiError(403, "forbidden", "当前角色等级不足，无法操作该知识库")


def list_readable_kbs(db: Session, user: User | None) -> list[KnowledgeBase]:
    """聊天与列表共用：只返回当前身份可读取的活动知识库。"""
    all_kbs = db.scalars(
        select(KnowledgeBase).where(KnowledgeBase.status == "active").order_by(KnowledgeBase.created_at)
    ).all()
    return [kb for kb in all_kbs if can_read_kb(kb, user)]


def list_operable_kbs(db: Session, user: User | None) -> list[KnowledgeBase]:
    """管理端只展示当前角色可维护的知识库。"""
    all_kbs = db.scalars(
        select(KnowledgeBase).where(KnowledgeBase.status == "active").order_by(KnowledgeBase.created_at)
    ).all()
    return [kb for kb in all_kbs if can_operate_kb(kb, user)]
