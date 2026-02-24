from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi_app.models.conversation import Conversation


async def get_conversation_by_id(
    db: AsyncSession, conversation_id: str
) -> Conversation | None:
    result = await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    return result.scalar_one_or_none()


async def list_conversations_by_user_id(
    db: AsyncSession,
    user_id: str,
    limit: int = 20,
) -> list[Conversation]:
    stmt = (
        select(Conversation)
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def create_conversation(
    db: AsyncSession,
    conversation_id: str,
    user_id: str,
    name: str | None = None,
    status: str | None = None,
    is_deleted: bool | None = False,
    dialogue_count: int | None = 0,
) -> Conversation:
    conversation = Conversation(
        id=conversation_id,
        user_id=user_id,
        name=name,
        status=status,
        is_deleted=is_deleted,
        dialogue_count=dialogue_count,
    )
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return conversation
