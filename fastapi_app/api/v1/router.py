from fastapi import APIRouter

from fastapi_app.api.v1.endpoints.chat import router as chat_router
from fastapi_app.api.v1.endpoints.game import router as game_router
from fastapi_app.api.v1.endpoints.health import router as health_router
from fastapi_app.api.v1.endpoints.user import router as user_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(chat_router)
api_router.include_router(game_router)
api_router.include_router(user_router)
