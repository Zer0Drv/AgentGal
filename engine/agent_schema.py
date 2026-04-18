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
    """character_factory agent 的结构化输出：一次返回完整骨架种子。

    soul 分成四段（identity / dynamic / behavior / voice），和美月模板对齐：
    - identity：单行公开身份
    - dynamic：长期关系循环 + 内外反差
    - behavior：情境→反应模式
    - voice：典型台词样例
    """

    role: str
    identity: str
    dynamic: str
    behavior: list[str] = Field(default_factory=list)
    voice: list[str] = Field(default_factory=list)
    status: dict[str, str] = Field(default_factory=dict)
    relations: dict[str, str] = Field(default_factory=dict)

    @field_validator("role", "identity", "dynamic", mode="before")
    @classmethod
    def trim_creation_fields(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("behavior", "voice", mode="before")
    @classmethod
    def trim_list_items(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [item.strip() if isinstance(item, str) else item for item in value]

    @field_validator("role", "identity", "dynamic")
    @classmethod
    def ensure_creation_fields_not_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("field cannot be empty")
        return value

    @field_validator("identity")
    @classmethod
    def normalize_identity_to_single_line(cls, value: str) -> str:
        return " ".join(value.split())


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
