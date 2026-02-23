from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="用户输入")
    user_id: str | None = Field(default=None, description="用户ID，不传则使用默认用户")
    conversation_id: str | None = Field(default=None, description="会话ID，不传则使用默认会话")


class AgentReply(BaseModel):
    agent: str
    content: str
    valid: bool


class ChatResponse(BaseModel):
    narrator: str | None = None
    targets: list[str] = Field(default_factory=list)
    replies: list[AgentReply] = Field(default_factory=list)
