from pydantic import BaseModel, Field
from typing import Literal


class RecentMessage(BaseModel):
    role: str
    author: str
    content: str


class StartGameResponse(BaseModel):
    mode: Literal["new", "continue"] = Field(description="new 或 continue")
    system_message: str
    opening: str | None = None
    recent_messages: list[RecentMessage] = Field(default_factory=list)


class StartOptionsResponse(BaseModel):
    has_save: bool
    recommended_action: Literal["new", "continue"]
    prompt: str
    recent_messages: list[RecentMessage] = Field(default_factory=list)


class StartActionRequest(BaseModel):
    action: Literal["new", "continue"] = Field(description="用户选择的新开局/继续")


class SaveGameResponse(BaseModel):
    success: bool
    save_path: str | None = None
    detail: str


class ResetGameResponse(BaseModel):
    success: bool
    opening: str | None = None
    detail: str


class EndGameResponse(BaseModel):
    success: bool
    detail: str
