# app/models/conversation.py
"""
Conversation model.
"""

from datetime import datetime
from typing import Optional, List, TYPE_CHECKING
from sqlalchemy import ForeignKey, Integer, String, DateTime, Text, Boolean, func, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from fastapi_app.db.base import Base

if TYPE_CHECKING:
    from .user import User
    from .message import Message


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, comment="对话ID")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now(), comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=func.now(),
        onupdate=func.now(),
        comment="更新时间",
    )
    user_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("users.id"), nullable=False, comment="用户ID"
    )
    name: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="对话名称")
    status: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, comment="状态"
    )
    is_deleted: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True, comment="是否删除"
    )
    dialogue_count: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="对话轮次"
    )

    # 关系
    user: Mapped["User"] = relationship("User", back_populates="conversations")
    messages: Mapped[List["Message"]] = relationship(
        "Message", back_populates="conversation"
    )

    # 索引
    __table_args__ = (
        Index("idx_user_conversations", user_id, created_at),
        Index("idx_conversation_status", status),
    )
