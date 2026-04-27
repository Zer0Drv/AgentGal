"""对话历史持久化 - 从 narrator raw JSONL 文件读写消息"""

import glob
import json
import os
from datetime import datetime

from shared.config import character_path


def load_conversation_history(
    *,
    limit: int | None = None,
    turns: int | None = None,
) -> list:
    """从 narrator 的 raw/ 目录加载最近的对话历史（跨所有日期的 jsonl 文件）。

    narrator 拥有上帝视角，包含所有消息。返回原始消息列表（未过滤、未格式化）。

    Args:
        limit: 按消息数返回最近 N 条（UI 显示等场景）。
        turns: 按 turn 号返回最近 N 个 turn 的所有消息（LLM 上下文场景，基于 `message['turn']`）。
        都不传 → 返回全部历史；同时传 → 抛 ValueError（语义冲突）。

    Returns:
        消息列表，按时间正序。每条是 dict（含 role / content / visible_to / turn 等字段）。
    """
    if limit is not None and turns is not None:
        raise ValueError("load_conversation_history: limit 与 turns 互斥")

    raw_dir = character_path("narrator", "raw")
    if not os.path.exists(raw_dir):
        return []

    jsonl_files = sorted(glob.glob(f"{raw_dir}/*.jsonl"))
    if not jsonl_files:
        return []

    if (limit is not None and limit <= 0) or (turns is not None and turns <= 0):
        return []

    # 从最新日期往回读，达成上限即停，避免每轮全量扫旧日志
    collected: list = []
    threshold_turn: int | None = None
    for filepath in reversed(jsonl_files):
        file_messages: list = []
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    file_messages.append(json.loads(stripped))
                except json.JSONDecodeError:
                    continue
        if not file_messages:
            continue

        if turns is not None and threshold_turn is None:
            max_turn = max((int(m.get("turn") or 0) for m in file_messages), default=0)
            threshold_turn = max(0, max_turn - turns + 1)

        collected = file_messages + collected

        if limit is not None and len(collected) >= limit:
            return collected[-limit:]
        if turns is not None and threshold_turn is not None:
            first_turn = min((int(m.get("turn") or 0) for m in file_messages), default=0)
            if first_turn <= threshold_turn:
                break

    if turns is not None and threshold_turn is not None:
        return [m for m in collected if int(m.get("turn") or 0) >= threshold_turn]
    return collected


async def append_message(message: dict) -> None:
    """将一条消息追加到 narrator 的 raw JSONL 文件（单一数据源）。"""
    date = datetime.now().strftime("%Y-%m-%d")
    raw_path = character_path("narrator", "raw", f"{date}.jsonl")
    os.makedirs(os.path.dirname(raw_path), exist_ok=True)
    with open(raw_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(message, ensure_ascii=False) + "\n")
