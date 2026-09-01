"""JWT 与 Refresh Token 工具。Refresh Token 原文只进入 HttpOnly Cookie。"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from app.config import settings

ALGORITHM = "HS256"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _secret() -> str:
    if len(settings.jwt_secret) < 32:
        raise ValueError("JWT_SECRET 未配置或长度不足 32 位")
    return settings.jwt_secret


def create_access_token(user_id: str) -> str:
    now = _now()
    return jwt.encode(
        {
            "sub": user_id,
            "type": "access",
            "iat": now,
            "exp": now + timedelta(minutes=settings.jwt_access_token_minutes),
        },
        _secret(),
        algorithm=ALGORITHM,
    )


def decode_access_token(token: str) -> str:
    payload = jwt.decode(token, _secret(), algorithms=[ALGORITHM])
    if payload.get("type") != "access" or not isinstance(payload.get("sub"), str):
        raise jwt.InvalidTokenError("无效的 access token")
    return payload["sub"]


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def refresh_expiry_naive() -> datetime:
    return (_now() + timedelta(days=settings.jwt_refresh_token_days)).replace(tzinfo=None)
