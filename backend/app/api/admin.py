"""系统管理员：用户状态与角色管理。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import user_out
from app.api.errors import BadRequestError, NotFoundError
from app.auth.dependencies import require_roles
from app.models.database import Role, User, UserRole
from app.models.schemas import UserOut, UserRolesUpdate, UserStatusUpdate
from app.store.db import get_db

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", response_model=list[UserOut])
def list_users(
    _admin: User = Depends(require_roles("admin")), db: Session = Depends(get_db)
) -> list[UserOut]:
    return [user_out(user) for user in db.scalars(select(User).order_by(User.created_at.desc()))]


@router.put("/users/{user_id}/status", response_model=UserOut)
def update_user_status(
    user_id: str,
    body: UserStatusUpdate,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> UserOut:
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError("user_not_found", "用户不存在")
    if user.id == admin.id and body.status != "active":
        raise BadRequestError("cannot_disable_self", "不能禁用当前管理员账号")
    user.status = body.status
    db.commit()
    db.refresh(user)
    return user_out(user)


@router.put("/users/{user_id}/roles", response_model=UserOut)
def update_user_roles(
    user_id: str,
    body: UserRolesUpdate,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
) -> UserOut:
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError("user_not_found", "用户不存在")
    roles = db.scalars(select(Role).where(Role.code.in_(body.roles))).all()
    if len(roles) != len(set(body.roles)):
        raise BadRequestError("role_not_found", "存在无效角色")
    if user.id == admin.id and "admin" not in {role.code for role in roles}:
        raise BadRequestError("cannot_remove_own_admin", "不能移除当前管理员的 admin 角色")
    db.query(UserRole).filter(UserRole.user_id == user.id).delete()
    for role in roles:
        db.add(UserRole(user_id=user.id, role_id=role.id))
    db.commit()
    db.refresh(user)
    return user_out(user)
