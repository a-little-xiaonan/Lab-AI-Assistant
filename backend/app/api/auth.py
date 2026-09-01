"""注册、登录、刷新与登出。"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import ApiError, ConflictError
from app.auth.dependencies import get_current_user
from app.auth.jwt import (
    create_access_token,
    hash_refresh_token,
    new_refresh_token,
    refresh_expiry_naive,
)
from app.auth.password import hash_password, verify_password
from app.config import settings
from app.models.database import RefreshToken, Role, User, UserRole, utcnow
from app.models.schemas import LoginRequest, RegisterRequest, TokenOut, UserOut
from app.store.db import get_db

router = APIRouter(prefix="/auth", tags=["auth"])
REFRESH_COOKIE = "rag_refresh_token"


def user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        nickname=user.nickname,
        email=user.email,
        roles=[r.code for r in user.roles],
        status=user.status,
        created_at=user.created_at,
    )


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        max_age=settings.jwt_refresh_token_days * 24 * 3600,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/api/auth",
    )


def _issue_tokens(db: Session, user: User, response: Response) -> TokenOut:
    raw_refresh = new_refresh_token()
    db.add(
        RefreshToken(
            id=f"rt_{uuid4().hex[:16]}",
            user_id=user.id,
            token_hash=hash_refresh_token(raw_refresh),
            expires_at=refresh_expiry_naive(),
        )
    )
    user.last_login_at = utcnow()
    db.commit()
    _set_refresh_cookie(response, raw_refresh)
    return TokenOut(access_token=create_access_token(user.id), user=user_out(user))


@router.post("/register", response_model=UserOut, status_code=201)
def register(body: RegisterRequest, db: Session = Depends(get_db)) -> UserOut:
    username = body.username.strip().lower()
    if db.scalar(select(User).where(User.username == username)):
        raise ConflictError("username_taken", "该用户名已被使用")
    if body.email and db.scalar(select(User).where(User.email == body.email.strip().lower())):
        raise ConflictError("email_taken", "该邮箱已被使用")
    student = db.scalar(select(Role).where(Role.code == "student"))
    if student is None:
        raise ApiError(500, "roles_not_initialized", "系统角色尚未初始化")
    user = User(
        id=f"user_{uuid4().hex[:16]}",
        username=username,
        password_hash=hash_password(body.password),
        nickname=body.nickname.strip(),
        email=body.email.strip().lower() if body.email else None,
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=student.id))
    db.commit()
    db.refresh(user)
    return user_out(user)


@router.post("/login", response_model=TokenOut)
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)) -> TokenOut:
    user = db.scalar(select(User).where(User.username == body.username.strip().lower()))
    if user is None or not verify_password(body.password, user.password_hash):
        raise ApiError(401, "invalid_credentials", "用户名或密码错误")
    if user.status != "active" or user.deleted_at is not None:
        raise ApiError(403, "account_unavailable", "账号已被禁用或删除")
    return _issue_tokens(db, user, response)


@router.post("/refresh", response_model=TokenOut)
def refresh(request: Request, response: Response, db: Session = Depends(get_db)) -> TokenOut:
    raw_refresh = request.cookies.get(REFRESH_COOKIE)
    if not raw_refresh:
        raise ApiError(401, "refresh_required", "登录已过期，请重新登录")
    row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw_refresh)))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        raise ApiError(401, "invalid_refresh_token", "登录已过期，请重新登录")
    user = db.get(User, row.user_id)
    if user is None or user.status != "active" or user.deleted_at is not None:
        raise ApiError(401, "account_unavailable", "账号不可用，请重新登录")
    row.revoked_at = now
    db.commit()
    return _issue_tokens(db, user, response)


@router.post("/logout", status_code=204)
def logout(request: Request, db: Session = Depends(get_db)) -> Response:
    raw_refresh = request.cookies.get(REFRESH_COOKIE)
    if raw_refresh:
        row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw_refresh)))
        if row is not None and row.revoked_at is None:
            row.revoked_at = utcnow()
            db.commit()
    # 不返回 FastAPI 注入的临时 Response：它的 status_code 可能为 None，
    # Uvicorn 记录访问日志时会按 %d 格式化并报错。
    response = Response(status_code=204)
    response.delete_cookie(REFRESH_COOKIE, path="/api/auth")
    return response


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return user_out(user)
