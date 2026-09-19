"""问答会话路由聚合。"""
from fastapi import APIRouter

from app.api.conversation import chat, feedback, memory

router = APIRouter()
router.include_router(chat.router)
router.include_router(memory.router)
router.include_router(feedback.router)
