"""记忆检索查询构造与召回拼装。"""

from __future__ import annotations

import re

from engine.config import VECTOR_SEARCH_LIMIT
from log_config.memory import memory_logger
from memory.vector_store import vector_store


_SCENE_FIELD_RE = re.compile(r"^\*{0,2}(时间|地点|在场)\*{0,2}：\s*(.*)$")


def _build_retrieval_scene_summary(scene_summary: str) -> str:
    """将 narrator 场景摘要裁剪成适合检索的轻量结构信息。"""
    if not scene_summary or not scene_summary.strip():
        return ""

    time_line = ""
    location_line = ""
    present_lines: list[str] = []
    in_present_section = False

    for raw_line in scene_summary.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        field_match = _SCENE_FIELD_RE.match(line)
        if field_match:
            field, value = field_match.groups()
            value = value.strip()
            if field == "时间":
                time_line = f"时间：{value}" if value else "时间："
            elif field == "地点":
                location_line = f"地点：{value}" if value else "地点："
            elif field == "在场":
                in_present_section = True
            continue

        if in_present_section and line.startswith("-"):
            present = line[1:].strip()
            if present and "不在场" not in present:
                present_lines.append(present)
            continue

        if in_present_section:
            in_present_section = False

    parts: list[str] = []
    if time_line:
        parts.append(time_line)
    if location_line:
        parts.append(location_line)
    if present_lines:
        parts.append("在场：")
        parts.extend(present_lines)
    return "\n".join(parts)


def build_search_query(agent_name: str, user_input: str, scene_summary: str = "") -> str:
    """构建基础 RAG query。"""
    del agent_name

    parts = [user_input, _build_retrieval_scene_summary(scene_summary)]

    return "\n".join(part.strip() for part in parts if part and part.strip())


def build_search_queries(
    agent_name: str,
    latest_user_input: str,
    scene_summary: str = "",
) -> tuple[str, str]:
    """为 vector / BM25 构造查询。"""
    vector_query = build_search_query(agent_name, latest_user_input, scene_summary)
    bm25_query = vector_query
    return vector_query, bm25_query


def search_memories(agent_name: str, vector_query: str, bm25_query: str = "") -> str:
    """执行检索并格式化记忆块。"""
    results = vector_store.search(
        agent_name,
        vector_query,
        limit=VECTOR_SEARCH_LIMIT,
        kind="memory",
        bm25_query=bm25_query or None,
    )
    memories = [r["content"].strip() for r in results if r["content"].strip()]
    return "\n\n---\n\n".join(memories) if memories else "（无相关记忆）"


def build_memory_prefix(
    agent_name: str,
    user_input: str,
    scene_summary: str = "",
) -> str:
    """组装 `<relevant_memories>` 块。"""
    if agent_name == "narrator":
        return ""

    vector_query, bm25_query = build_search_queries(agent_name, user_input, scene_summary)
    memory_logger.info(
        "[Retrieval] agent=%s\nuser_input=%s\nvector_query=%s\nbm25_query=%s",
        agent_name,
        user_input.strip() or "（空）",
        vector_query or "（空）",
        bm25_query or "（空）",
    )
    relevant = search_memories(agent_name, vector_query, bm25_query)
    return f"<relevant_memories>\n{relevant}\n</relevant_memories>"
