"""身份认证与角色治理路由聚合。"""
from fastapi import APIRouter

from app.api.identity import auth, role_applications, users

router = APIRouter()
router.include_router(auth.router)
router.include_router(users.router)
router.include_router(role_applications.router)
