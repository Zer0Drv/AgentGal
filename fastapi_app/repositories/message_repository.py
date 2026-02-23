from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi_app.models.message import Message


async def get_message_by_id(db: AsyncSession, message_id: str) -> Message | None:
    result = await db.execute(select(Message).where(Message.id == message_id))
    return result.scalar_one_or_none()


async def list_messages_by_conversation_id(
    db: AsyncSession,
    conversation_id: str,
    limit: int = 100,
) -> list[Message]:
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def create_message(
    db: AsyncSession,
    message_id: str,
    conversation_id: str,
    user_id: str,
    role: str | None = None,
    content: str | None = None,
    visible_to: str | None = None,
) -> Message:
    message = Message(
        id=message_id,
        conversation_id=conversation_id,
        user_id=user_id,
        role=role,
        content=content,
        visible_to=visible_to,
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)
    return message
