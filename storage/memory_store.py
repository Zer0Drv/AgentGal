"""memory.jsonl / understanding.jsonl 的 JSONL 持久化。

机制层：负责领域实体（models）与磁盘 JSONL 之间的读写，不含检索 / 整理策略。
"""

import uuid
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from models import EpisodeMemory, Understanding
from shared.config import character_path


# ===== memory.jsonl 读写 =====


def memory_jsonl_path(agent_name: str) -> Path:
    """返回角色的 memory.jsonl 路径（不负责存在性检查）。"""
    return Path(character_path(agent_name, "memory.jsonl"))


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
    valid = [
        r.model_copy(
            update={
                "memory_owner": agent_name,
                "id": r.id or uuid.uuid4().hex,
            }
        )
        for r in records
        if r.content.strip()
    ]
    if not valid:
        return []
    path = memory_jsonl_path(agent_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for record in valid:
            f.write(serialize_episode(record) + "\n")
    return valid


# ===== understanding.jsonl 读写 =====


def understanding_jsonl_path(agent_name: str) -> Path:
    """返回角色的 understanding.jsonl 路径（不负责存在性检查）。"""
    return Path(character_path(agent_name, "understanding.jsonl"))


def read_understandings(agent_name: str) -> dict[str, Understanding]:
    """读取 understanding.jsonl，返回 {id: Understanding}；不存在返回空字典；非法行跳过。"""
    path = understanding_jsonl_path(agent_name)
    if not path.exists():
        return {}
    result: dict[str, Understanding] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                u = Understanding.model_validate_json(stripped)
                result[u.id] = u
            except ValidationError:
                pass
    return result


def write_understandings(
    agent_name: str,
    understandings: dict[str, Understanding],
) -> None:
    """全量写回 understanding.jsonl；空字典时删除文件。"""
    path = understanding_jsonl_path(agent_name)
    if not understandings:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for u in understandings.values():
            record = u.model_copy(update={"memory_owner": agent_name})
            f.write(record.model_dump_json() + "\n")
