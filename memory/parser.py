"""memory 数据结构与解析工具 - EpisodeMemory、JSONL I/O、日期工具"""

import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from shared.config import character_path


# ===== EpisodeMemory：长期记忆单条记录 =====


class EpisodeMemory(BaseModel):
    """memory.jsonl 的一行记录，对应一条结构化长期记忆事件。

    字段布局与 MemoryMergeEvent / OffstageMemoryBlock 对齐，便于直接写盘。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    date: str = ""
    time: str = ""
    location: str = ""
    participants: str = ""
    keywords: list[str] = Field(default_factory=list)
    importance: int = 3
    content: str = ""
    memory_owner: str = ""
    title: str = ""

    @field_validator("keywords", mode="before")
    @classmethod
    def _clean_keywords(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(k).strip() for k in value if str(k).strip()]

    @field_validator("importance", mode="before")
    @classmethod
    def _clamp_importance(cls, value: object) -> int:
        try:
            return max(1, min(5, int(value)))
        except (TypeError, ValueError):
            return 3


def parse_jsonl_line(line: str) -> EpisodeMemory | None:
    """解析一行 JSONL 为 EpisodeMemory；格式非法返回 None。"""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        return EpisodeMemory.model_validate_json(stripped)
    except ValidationError:
        return None


def serialize_episode(episode: EpisodeMemory) -> str:
    """将 EpisodeMemory 序列化为 JSONL 一行（不含换行）。"""
    return episode.model_dump_json()


# ===== memory.jsonl 读写 =====


def memory_jsonl_path(agent_name: str) -> Path:
    """返回角色的 memory.jsonl 路径（不负责存在性检查）。"""
    return Path(character_path(agent_name, "memory.jsonl"))


def read_memory_jsonl(agent_name: str) -> list[EpisodeMemory]:
    """按文件顺序读取 memory.jsonl；不存在或全空返回空列表；非法行跳过。"""
    path = memory_jsonl_path(agent_name)
    if not path.exists():
        return []
    records: list[EpisodeMemory] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            record = parse_jsonl_line(line)
            if record is not None:
                records.append(record)
    return records


def append_memory_records(
    agent_name: str,
    records: Iterable[EpisodeMemory],
) -> list[EpisodeMemory]:
    """将一组 EpisodeMemory 追加写入 memory.jsonl。

    Returns:
        实际写入的记录列表（过滤掉 content 为空的条目，保持原顺序）。
    """
    valid = [r for r in records if r.content.strip()]
    if not valid:
        return []
    path = memory_jsonl_path(agent_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for record in valid:
            f.write(serialize_episode(record) + "\n")
    return valid


# ===== 文本规范化 =====


def normalize(content: str) -> str:
    """修复常见格式问题：字面\\n、日期标题不规范、多余空行。"""
    content = content.replace("\\n", "\n")
    content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
    lines = content.split("\n")
    out = []
    for line in lines:
        stripped = line.strip()
        m = re.match(r"^(?:#{1,2}\s*|\*\*)?(\d{1,2}月\d{1,2}日)(?:\*\*)?(?:\s.*)?$", stripped)
        if m:
            out.append(f"## {m.group(1)}")
        elif re.match(r"^(?:\*\*(时间|地点|在场|关键词|重要度|内容)\*\*|(时间|地点|在场|关键词|重要度|内容))：", stripped):
            out.append(f"- {stripped}")
        else:
            out.append(line)
    content = "\n".join(out)
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()


# ===== 字段提取 =====


def extract_status_field(status_text: str, field_name: str) -> str:
    """从 status.md 文本中提取指定 ## 字段的值。

    Args:
        status_text: status.md 的完整文本内容
        field_name: 要提取的字段名（如 "叙事焦点"、"心境"）

    Returns:
        字段内容，未找到返回空字符串
    """
    pattern = re.compile(
        rf"^##\s+{re.escape(field_name)}\s*\n(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(status_text)
    if not m:
        return ""
    return m.group(1).strip()


# ===== 游戏日期工具 =====


def parse_cn_date(date_text: str) -> tuple[int, int] | None:
    """解析中文日期格式（如 4月3日 / 4月3日 08:00）为元组 (月, 日)。"""
    m = re.search(r"(\d{1,2})月(\d{1,2})日", (date_text or "").strip())
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    try:
        datetime(2000, month, day)
    except ValueError:
        return None
    return month, day


def canonical_cn_date(date_text: str) -> str | None:
    """将中文日期文本规范化为 X月X日。"""
    parsed = parse_cn_date(date_text)
    if parsed is None:
        return None
    month, day = parsed
    return f"{month}月{day}日"


def game_day_number(date_text: str) -> int | None:
    """将游戏日期映射到固定年份中的 day-of-year，便于比较天数差。"""
    parsed = parse_cn_date(date_text)
    if parsed is None:
        return None
    month, day = parsed
    return datetime(2000, month, day).timetuple().tm_yday


def game_day_diff(current_date: str, past_date: str) -> int | None:
    """返回两个游戏日期的天数差；若无法解析返回 None。"""
    current_day = game_day_number(current_date)
    past_day = game_day_number(past_date)
    if current_day is None or past_day is None:
        return None
    return max(current_day - past_day, 0)
