"""应用 API 总路由；业务子包只在此处完成一次聚合。"""
from fastapi import APIRouter

from app.api.conversation.router import router as conversation_router
from app.api.identity.router import router as identity_router
from app.api.knowledge.router import router as knowledge_router
from app.api.operations.router import router as operations_router
from app.api.system.router import router as system_router

api_router = APIRouter()
api_router.include_router(system_router)
api_router.include_router(identity_router)
api_router.include_router(conversation_router)
api_router.include_router(knowledge_router)
api_router.include_router(operations_router)
