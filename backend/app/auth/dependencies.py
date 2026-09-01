"""FastAPI 认证依赖：身份来自 Bearer Token，不接受客户端 user_id。"""
from __future__ import annotations

from collections.abc import Callable

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.auth.jwt import decode_access_token
from app.models.database import User
from app.store.db import get_db

_bearer = HTTPBearer(auto_error=False)


def get_optional_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User | None:
    if credentials is None:
        return None
    try:
        user_id = decode_access_token(credentials.credentials)
    except (jwt.PyJWTError, ValueError):
        raise ApiError(401, "invalid_token", "登录状态无效或已过期")
    user = db.get(User, user_id)
    if user is None or user.status != "active" or user.deleted_at is not None:
        raise ApiError(401, "account_unavailable", "账号不可用，请重新登录")
    return user


def get_current_user(user: User | None = Depends(get_optional_current_user)) -> User:
    if user is None:
        raise ApiError(401, "authentication_required", "请先登录")
    return user


def has_role(user: User, *codes: str) -> bool:
    return any(role.code in codes for role in user.roles)


def require_roles(*codes: str) -> Callable:
    def _dependency(user: User = Depends(get_current_user)) -> User:
        if not has_role(user, *codes):
            raise ApiError(403, "forbidden", "当前账号没有执行此操作的权限")
        return user

    return _dependency
