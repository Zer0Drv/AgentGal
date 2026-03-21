"""对话历史读写 - narrator raw JSONL 的加载接口"""

import glob
import json
import os

from engine.config import character_path


def load_conversation_history(limit: int | None = 10) -> list:
    """从 narrator 的 raw/ 目录加载最近的对话历史（跨所有日期的 jsonl 文件）

    narrator 拥有上帝视角，包含所有消息。返回原始消息列表（未过滤、未格式化）。

    Args:
        limit: 返回最近多少条消息；传 None 时返回全部

    Returns:
        最近 limit 条消息的列表，每条是 dict（包含 role, content, visible_to 等字段）
    """
    raw_dir = character_path("narrator", "raw")
    if not os.path.exists(raw_dir):
        return []

    jsonl_files = sorted(glob.glob(f"{raw_dir}/*.jsonl"))
    if not jsonl_files:
        return []

    all_messages = []
    for filepath in jsonl_files:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        all_messages.append(json.loads(line.strip()))
                    except json.JSONDecodeError:
                        continue

    if limit is None:
        return all_messages

    return all_messages[-limit:]
