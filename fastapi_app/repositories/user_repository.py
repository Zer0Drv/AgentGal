from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi_app.models.user import User


async def get_user_by_id(db: AsyncSession, user_id: str) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    result = await db.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def list_users(db: AsyncSession, limit: int = 100) -> list[User]:
    stmt = select(User).order_by(User.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def create_user(
    db: AsyncSession,
    username: str,
    user_id: str | None = None,
    password: str | None = None,
    access_token: str | None = None,
) -> User:
    user = User(
        id=user_id or uuid4().hex,
        username=username,
        password=password,
        access_token=access_token,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def update_last_login(
    db: AsyncSession,
    user: User,
    last_login: datetime | None = None,
) -> User:
    user.last_login = last_login or datetime.now()
    await db.commit()
    await db.refresh(user)
    return user


async def update_access_token(
    db: AsyncSession,
    user: User,
    access_token: str | None,
) -> User:
    user.access_token = access_token
    await db.commit()
    await db.refresh(user)
    return user
