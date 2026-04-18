"""所有 Agent 的结构化输出类型（对话 + 记忆整理）。"""

from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator


MAX_CHOICE_CHARS = 50
ChoiceText = Annotated[str, Field(max_length=MAX_CHOICE_CHARS)]


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
    relations: dict[str, str] = Field(default_factory=dict)


class NarratorStatus(BaseModel):
    """narrator status 的强类型字段，防止 LLM 将多个字段混写成单个 key 的值导致 JSON 非法。"""

    场景: str = ""
    角色位置: str = ""
    当前时间: str = ""
    叙事焦点: str = ""


class NewCharacterSpec(BaseModel):
    """narrator 请求动态生成新角色时的最小锚点。"""

    character_id: str = Field(validation_alias=AliasChoices("character_id", "name"))
    display_name: str = ""
    relation_to: str
    relation_description: str
    background_hint: str = ""
    initial_location: str = ""


class NarratorOutput(BaseModel):
    targets: list[str]
    content: str
    new_characters: list[NewCharacterSpec] = Field(default_factory=list)


class NewCharacterCreation(BaseModel):
    """character_factory agent 的结构化输出：一次返回 soul / status / relations 种子。"""

    soul: str
    status: dict[str, str] = Field(default_factory=dict)
    relations: dict[str, str] = Field(default_factory=dict)


class OffstageMemoryBlock(BaseModel):
    """离场追补：角色重新登场时补录的单条压缩记忆。

    content 是第一人称的 `- **时间/地点/在场**：...` 单条摘要，
    格式与角色正常输出的 memory 一致，不应包含"离场"等标签。
    """

    date: str
    content: str


class StateUpdaterOutput(BaseModel):
    status: NarratorStatus = Field(default_factory=NarratorStatus)
    triggered: list[str] = Field(default_factory=list)
    add_event: list[str] = Field(default_factory=list)


class ChoicesOutput(BaseModel):
    choices: list[ChoiceText]

    @field_validator("choices", mode="before")
    @classmethod
    def trim_choice_lengths(cls, choices: object) -> object:
        if not isinstance(choices, list):
            return choices
        return [
            choice.strip()[:MAX_CHOICE_CHARS] if isinstance(choice, str) else choice
            for choice in choices
        ]


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


# ---------------------------------------------------------------------------
# 世界调度（schedule.json）
# ---------------------------------------------------------------------------


Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TimeBucket = Literal["上午", "下午", "晚上", "全天"]


class CharacterScheduleSlot(BaseModel):
    days: list[Weekday]
    time: TimeBucket
    location: str


class CharacterSchedulePeriod(BaseModel):
    start: str
    end: str
    name: str = ""
    slots: list[CharacterScheduleSlot] = Field(default_factory=list)


class CharacterSchedule(BaseModel):
    periods: list[CharacterSchedulePeriod] = Field(default_factory=list)
