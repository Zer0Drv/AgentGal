"""所有 Agent 的结构化输出类型（对话 + 记忆整理）。"""

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 对话类
# ---------------------------------------------------------------------------


class CharacterOutput(BaseModel):
    content: str
    memory: str
    status: dict[str, str] = Field(default_factory=dict)
    player: dict[str, str] = Field(default_factory=dict)
    triggered: list[str] = Field(default_factory=list)
    add_event: list[str] = Field(default_factory=list)


class NarratorStatus(BaseModel):
    """narrator status 的强类型字段，防止 LLM 将多个字段混写成单个 key 的值导致 JSON 非法。"""

    场景: str = ""
    角色位置: str = ""
    当前时间: str = ""
    叙事焦点: str = ""
    关系现状: str = ""


class NarratorOutput(BaseModel):
    targets: list[str]
    content: str
    status: NarratorStatus = Field(default_factory=NarratorStatus)
    triggered: list[str] = Field(default_factory=list)
    add_event: list[str] = Field(default_factory=list)


class ChoicesOutput(BaseModel):
    choices: list[str]


# ---------------------------------------------------------------------------
# 记忆整理类
# ---------------------------------------------------------------------------


class MemoryMergeEvent(BaseModel):
    date: str
    time: str
    location: str
    participants: str
    content: str


class MemoryMergeOutput(BaseModel):
    events: list[MemoryMergeEvent]


class MemoryMetadataItem(BaseModel):
    time: str
    keywords: list[str]
    importance: int


class MemoryMetadataOutput(BaseModel):
    items: list[MemoryMetadataItem]


class GrowthExtractOutput(BaseModel):
    updates: list[str] = Field(default_factory=list)


class GrowthDedupOutput(BaseModel):
    entries: list[str] = Field(default_factory=list)

