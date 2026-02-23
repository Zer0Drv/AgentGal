import os
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from jose import jwt, JWTError

from sqlalchemy.ext.asyncio import AsyncSession

from fastapi_app.db.session import get_db
from fastapi_app.models.user import User
from fastapi_app.repositories import get_user_by_username

# 优先使用内置实现的 pbkdf2_sha256，避免缺少 bcrypt C 后端时报错。
# 同时保留 bcrypt 用于兼容历史密码哈希的校验。
pwd_context = CryptContext(schemes=["pbkdf2_sha256", "bcrypt"], deprecated="auto")
ALGORITHM = "HS256"
SECRET_KEY = os.getenv("JWT_SECRET_KEY") or os.getenv("KEY") or "change-this-secret-key"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))


def verify_password(plain_password: str, hashed_password: str | None) -> bool:
    """
    验证明文密码 vs hash密码
    :param plain_password: 明文密码
    :param hashed_password: hash密码
    :return:
    """
    if not hashed_password:
        return False
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """
    加密明文
    :param password: 明文密码
    :return:
    """
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """
    生成token
    :param data: 字典
    :param expires_delta: 有效时间
    :return:
    """
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


async def check_token(
    credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
    db: AsyncSession = Depends(get_db),
) -> User:
    """检查用户token"""
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if not username:
            raise HTTPException(detail="token认证失败", status_code=401)
        user = await get_user_by_username(db, username)
        if user is None:
            raise HTTPException(detail="用户不存在", status_code=401)
        if user.access_token != token:
            raise HTTPException(detail="token认证失败", status_code=401)
        return user
    except JWTError:
        raise HTTPException(detail="token认证失败", status_code=401)
