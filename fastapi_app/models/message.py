# app/models/message.py
"""
Message model.
"""

from datetime import datetime
from typing import Optional, TYPE_CHECKING
from sqlalchemy import ForeignKey, String, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from fastapi_app.db.base import Base

if TYPE_CHECKING:
    from .user import User
    from .conversation import Conversation

class Message(Base):
    __tablename__ = 'messages'
    
    id: Mapped[str] = mapped_column(
        String(225),
        primary_key=True,
        comment="消息ID"
    )
    conversation_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey('conversations.id'),
        nullable=False,
        comment="对话ID"
    )    
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=func.now(),
        comment="创建时间"
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        onupdate=func.now(),
        comment="更新时间"
    )
    user_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey('users.id'),
        nullable=False,
        comment="用户ID"
    )
    role: Mapped[str] = mapped_column(
        String(255),
        nullable=True,
        comment="角色"
    )
    content: Mapped[str] = mapped_column(
        String(255),
        nullable=True,
        comment="内容"
    )
    visible_to: Mapped[str] = mapped_column(
        String(255),
        nullable=True,
        comment="内容"
    )

    user: Mapped["User"] = relationship("User", back_populates="messages")
    conversation: Mapped["Conversation"] = relationship(
        "Conversation",
        back_populates="messages",
    )
