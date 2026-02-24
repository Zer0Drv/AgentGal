from typing import Dict, Any
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from loguru import logger

from app.api.dependencies import get_db
from app.core.config import settings
from app.db import AsyncSession, get_or_none
from app.models import User

token_router = APIRouter(tags=["token管理"])


# FastAPI 路由
async def verify_token(token: str) -> Dict[str, Any]:
    """
    验证 token 的有效性

    Args:
        token: 待验证的 token

    Returns:
        验证结果的字典
    """
    url = settings.rdp_authentication_url

    payload = {"token": token, "system": "heman"}
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Token verification failed: {e}")
        return {"error": "Token verification failed"}


@token_router.get("/")
async def get_token(token: str) -> RedirectResponse:
    """
    重定向到前端页面并携带 token

    Args:
        token: 认证 token

    Returns:
        重定向响应
    """
    params = urlencode({"token": token})
    redirect_url = f"{settings.frontend_url}?{params}"
    return RedirectResponse(redirect_url)


@token_router.get("/v1/getUserInfo")
async def get_current_user_info(
    token: str, db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """
    获取当前用户信息

    Args:
        token: JWT token
        db: 数据库会话

    Returns:
        用户信息字典
    """
    try:
        payload = jwt.decode(token, settings.KEY, algorithms=["HS256"])
        username: str = payload.get("sub")

        if not username:
            logger.warning("Token missing 'sub' claim")
            return _build_user_response(username, code=401)

        obj = await get_or_none(db, User, username=username)

        if obj is None:
            logger.info(f"User not found: {username}")
            return _build_user_response(username, code=401)

        return _build_user_response(username, code=200)

    except JWTError as e:
        logger.error(f"JWT decode error: {e}")
        # 在 JWT 错误时 username 可能不存在，使用默认值
        return _build_user_response("unknown", code=401)


def _build_user_response(username: str, code: int) -> Dict[str, Any]:
    """
    构建用户信息响应

    Args:
        username: 用户名
        code: 响应状态码

    Returns:
        标准格式的用户信息响应
    """
    return {
        "code": code,
        "data": {
            "username": username,
            "nickname": username,
            "rdpurl": settings.rdp_applist,
        },
    }
