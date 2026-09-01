"""表现层：角色情绪标签轨迹（emotions.jsonl）。

每回合角色输出 emotion 标签后追加一行：
    {"turn": int, "date": str, "time": str, "emotion": str, "reason": str}

emotion 允许带强度前缀（如"有点害羞"/"非常害怕"），拆解与表情参数映射见
app.emotion_mapper（表现层 → 可动模型）。本模块只负责存储与读取。
"""

from __future__ import annotations

import json
import os
from typing import Any

from repository.config import character_path
from repository.log_config.routing import routing_logger

_FILENAME = "emotions.jsonl"


def emotions_path(name: str) -> str:
    return character_path(name, _FILENAME)


def append_emotion(
    name: str,
    emotion: str,
    *,
    turn: int = 0,
    date: str = "",
    time: str = "",
    reason: str = "",
) -> None:
    """追加一条情绪记录（表现层轨迹）。"""
    path = emotions_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record = {
        "turn": int(turn or 0),
        "date": str(date).strip(),
        "time": str(time).strip(),
        "emotion": str(emotion).strip(),
        "reason": str(reason).strip(),
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_all_emotions(name: str) -> list[dict[str, Any]]:
    """读取全部情绪轨迹（按时间顺序）。"""
    path = emotions_path(name)
    if not os.path.exists(path):
        return []
    records: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                routing_logger.warning("[emotion_store] 跳过损坏行 %s", path)
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def read_recent_emotions(name: str, limit: int = 5) -> list[dict[str, Any]]:
    """读取最近 N 条情绪记录（新→旧）。"""
    records = read_all_emotions(name)
    if limit <= 0:
        return records
    return records[-limit:][::-1]
