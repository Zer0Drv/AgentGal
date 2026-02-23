"""
User model.
"""

from datetime import datetime
from typing import Optional, List
from sqlalchemy import String, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fastapi_app.db.base import Base

class User(Base):
    __tablename__ = 'users'
    
    id: Mapped[str] = mapped_column(
        String(225), 
        primary_key=True,
        comment="用户ID"
    )
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        default=func.now(),
        comment="创建时间"
    )
    access_token: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="访问令牌"
    )
    username: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="用户名"
    )
    password: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="密码"
    )
    last_login: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="最后登录时间"
    )
    
    # 关系
    conversations: Mapped[List["Conversation"]] = relationship(
        "Conversation", 
        back_populates="user"
    )
    messages: Mapped[List["Message"]] = relationship(
        "Message", 
        back_populates="user"
    )