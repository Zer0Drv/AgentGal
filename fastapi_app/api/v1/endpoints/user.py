from datetime import datetime
from typing import Generic, TypeVar

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi_app.core import security
from fastapi_app.db.session import get_db
from fastapi_app.models.user import User
from fastapi_app.repositories import (
    create_user,
    get_user_by_id,
    get_user_by_username,
    update_access_token,
    update_last_login,
)

router = APIRouter(prefix="/User", tags=["User"])

T = TypeVar("T")


class Result(BaseModel, Generic[T]):
    success: bool
    message: str = ""
    data: T | None = None


class UserLogin(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1, max_length=255)


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1, max_length=255)


class UserUpdate(BaseModel):
    password: str = Field(..., min_length=1, max_length=255)


class UserOut(BaseModel):
    id: str
    username: str
    created_at: datetime | None = None
    last_login: datetime | None = None

    model_config = {"from_attributes": True}


class LoginData(BaseModel):
    token: str
    user: UserOut


@router.post("/login", summary="登录", response_model=Result[LoginData])
async def login(
    instance: UserLogin,
    db: AsyncSession = Depends(get_db),
) -> Result[LoginData]:
    user = await get_user_by_username(db, instance.username)
    if user is None or not security.verify_password(instance.password, user.password):
        return Result[LoginData](success=False, message="账号或密码错误")

    token = security.create_access_token(data={"sub": user.username})
    await update_access_token(db, user, token)
    await update_last_login(db, user, datetime.now())
    return Result[LoginData](
        success=True,
        message="登录成功",
        data=LoginData(token=token, user=UserOut.model_validate(user)),
    )


@router.post("/register", summary="注册", response_model=Result[UserOut])
async def register(
    instance: UserCreate,
    db: AsyncSession = Depends(get_db),
) -> Result[UserOut]:
    exists = await get_user_by_username(db, instance.username)
    if exists:
        return Result[UserOut](success=False, message="用户已存在")

    user = await create_user(
        db,
        username=instance.username,
        password=security.get_password_hash(instance.password),
    )
    return Result[UserOut](
        success=True,
        message="注册成功",
        data=UserOut.model_validate(user),
    )


@router.patch("/{user_id}", summary="更新密码", response_model=Result[UserOut])
async def update_user_by_id(
    user_id: str,
    instance: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(security.check_token),
) -> Result[UserOut]:
    if current_user.id != user_id:
        raise HTTPException(status_code=403, detail="无权修改其他用户")

    user = await get_user_by_id(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    user.password = security.get_password_hash(instance.password)
    await db.commit()
    await db.refresh(user)
    return Result[UserOut](
        success=True,
        message="密码更新成功",
        data=UserOut.model_validate(user),
    )
