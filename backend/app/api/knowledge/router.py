"""知识内容路由聚合。"""
from fastapi import APIRouter

from app.api.knowledge import documents, knowledge_bases, reviews

router = APIRouter()
router.include_router(knowledge_bases.router)
router.include_router(documents.router)
router.include_router(reviews.router)
