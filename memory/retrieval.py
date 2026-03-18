"""记忆检索查询构造与召回拼装。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from engine.config import VECTOR_SEARCH_LIMIT
from log_config.memory import memory_logger
from memory.file_ops import extract_status_field, read_agent_file
from memory.vector_store import vector_store


_LEADING_ACTION_RE = re.compile(r"^\s*(?:（[^）]{0,80}）|\([^)]{0,80}\)\s*)+")
_PURE_ACTION_RE = re.compile(r"^\s*(?:（[^）]{0,80}）|\([^)]{0,80}\))\s*$")


@dataclass
class _SceneFeatures:
    location_text: str = ""
    location_keywords: list[str] | None = None
    relation_text: str = ""


def _get_display_name(agent_name: str) -> str:
    """从 soul.md 提取中文显示名，回退到 agent_name。"""
    soul_content = read_agent_file(agent_name, "soul.md")
    role_match = re.search(r"<role>\s*([^\n<]+)", soul_content)
    if role_match:
        name_match = re.match(r"([\u4e00-\u9fff·]+)", role_match.group(1).strip())
        if name_match:
            return name_match.group(1)
    title_match = re.search(r"^#\s+(.+)$", soul_content, re.MULTILINE)
    if title_match:
        return title_match.group(1).strip()
    return agent_name


def _clean_spoken_text(text: str) -> str:
    """清理玩家原话中的动作描写，仅保留真正说出口的话。"""
    cleaned_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _PURE_ACTION_RE.match(line):
            continue
        line = _LEADING_ACTION_RE.sub("", line).strip()
        if line:
            cleaned_lines.append(line)
    cleaned = " ".join(cleaned_lines).strip()
    cleaned = re.sub(r"^(?:我…|我\.\.\.|我,|我，|这个，|这个,|呃，|呃,|\.\.\.|…|\s)+", "", cleaned)
    return cleaned.strip("，, ")


def _normalize_scene_location(location_text: str, display_name: str) -> str:
    """规范化场景地点，去掉角色 possessive 等噪音。"""
    location = (location_text or "").strip()
    if not location:
        return ""
    location = location.replace(f"{display_name}的", "")
    location = location.replace("，", "、").replace(",", "、")
    location = re.sub(r"\s+", "", location)
    location = re.sub(r"、+", "、", location).strip("、")
    return location


def _extract_location_keywords(location_text: str) -> list[str]:
    """从地点里提取少量稳定关键词，供 BM25 使用。"""
    keywords: list[str] = []
    for token in ["梧桐街", "咖啡馆", "路口", "车旁", "车", "电梯", "办公室", "走廊"]:
        if token == "车" and "车旁" in location_text:
            continue
        if token in location_text and token not in keywords:
            keywords.append(token)
    return keywords


def _extract_scene_features(agent_name: str, scene_summary: str) -> _SceneFeatures:
    """从 narrator 场景摘要中提取检索需要的骨架信息。"""
    display_name = _get_display_name(agent_name)
    location_match = re.search(r"\*\*地点\*\*：([^\n]+)", scene_summary)
    raw_location = location_match.group(1).strip() if location_match else ""
    location_text = _normalize_scene_location(raw_location, display_name)

    present_names: list[str] = []
    for name, position in re.findall(r"-\s*([^：\n]+)：([^\n]+)", scene_summary):
        name = name.strip()
        position = position.strip()
        if "不在场" in position:
            continue
        present_names.append(name)

    relation_text = ""
    others = [name for name in present_names if name != "玩家"]
    if present_names and "玩家" in present_names and others == [display_name]:
        relation_text = "独处"

    return _SceneFeatures(
        location_text=location_text,
        location_keywords=_extract_location_keywords(location_text),
        relation_text=relation_text,
    )


def _find_topic_anchor(cleaned_user_input: str, scene_features: _SceneFeatures, history: str) -> tuple[str, list[str]]:
    """从当前句子和最近历史中提取关系主题锚点。"""
    combined = "\n".join(
        part for part in [cleaned_user_input, history, scene_features.location_text] if part
    )

    if any(token in combined for token in ["约会", "误会"]):
        return "当前话题是两人是否在约会。", ["约会", "误会", "确认"]

    if any(token in combined for token in ["不叫", "随你叫", "顾总", "称呼", "名字"]):
        return "当前话题是两人称呼的变化。", ["称呼", "顾总", "名字"]

    if any(token in combined for token in ["太快", "后者", "跟我回去", "送我回去", "送你回去", "车旁", "路口"]):
        return "当前话题是送回去前的暧昧试探。", ["送回去", "暧昧", "试探", "太快"]

    if "咖啡馆" in combined or "豆子" in combined or "拿铁" in combined:
        return "当前话题是咖啡馆里的单独相处。", ["咖啡馆", "单独相处"]

    return "", []


def _build_vector_query(
    agent_name: str,
    cleaned_user_input: str,
    scene_features: _SceneFeatures,
    history: str,
) -> str:
    """构造给向量检索使用的紧凑语义摘要。"""
    display_name = _get_display_name(agent_name)
    relation = scene_features.relation_text or ""
    if scene_features.location_text and relation:
        intro = f"{display_name}和玩家在{scene_features.location_text}{relation}。"
    elif scene_features.location_text:
        intro = f"{display_name}和玩家在{scene_features.location_text}。"
    else:
        intro = f"{display_name}和玩家正在互动。"

    parts = [intro]
    if cleaned_user_input:
        parts.append(f"玩家说：\"{cleaned_user_input}\"")
    topic_anchor, _ = _find_topic_anchor(cleaned_user_input, scene_features, history)
    if topic_anchor:
        parts.append(topic_anchor)
    return "".join(parts)


def _build_bm25_query(
    agent_name: str,
    cleaned_user_input: str,
    scene_features: _SceneFeatures,
    history: str,
) -> str:
    """构造给 BM25 使用的关键词查询。"""
    display_name = _get_display_name(agent_name)
    keywords = [display_name, "玩家"]

    for token in scene_features.location_keywords or []:
        if token not in keywords:
            keywords.append(token)

    topic_anchor, topic_keywords = _find_topic_anchor(cleaned_user_input, scene_features, history)
    if (
        scene_features.relation_text
        and scene_features.relation_text not in keywords
        and topic_anchor in {"当前话题是两人是否在约会。", "当前话题是咖啡馆里的单独相处。"}
    ):
        keywords.append(scene_features.relation_text)

    for token in topic_keywords:
        if token not in keywords:
            keywords.append(token)

    return " ".join(keywords)


def build_search_query(agent_name: str, user_input: str, scene_summary: str = "") -> str:
    """构建基础 RAG query。

    narrator：玩家原话 + 叙事焦点 + 场景
    角色：玩家原话 + 旁白场景摘要
    """
    parts = [user_input]
    status = read_agent_file(agent_name, "status.md")

    if agent_name == "narrator":
        focus = extract_status_field(status, "叙事焦点")
        scene = extract_status_field(status, "场景")
        if focus:
            parts.append(focus)
        if scene:
            parts.append(scene)
    elif scene_summary:
        parts.append(scene_summary)

    return "\n".join(p for p in parts if p)


def build_search_queries(
    agent_name: str,
    latest_user_input: str,
    scene_summary: str = "",
    history: str = "",
) -> tuple[str, str]:
    """为 vector / BM25 各自构造更稳定的 query。"""
    if agent_name == "narrator":
        query = build_search_query(agent_name, latest_user_input, scene_summary)
        return query, query

    cleaned_user_input = _clean_spoken_text(latest_user_input)
    scene_features = _extract_scene_features(agent_name, scene_summary)
    vector_query = _build_vector_query(agent_name, cleaned_user_input, scene_features, history)
    bm25_query = _build_bm25_query(agent_name, cleaned_user_input, scene_features, history)
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
    history: str = "",
) -> str:
    """组装 `<relevant_memories>` 块。"""
    if agent_name == "narrator":
        return ""
    vector_query, bm25_query = build_search_queries(agent_name, user_input, scene_summary, history)
    cleaned_user_input = _clean_spoken_text(user_input)
    memory_logger.info(
        "[Retrieval] agent=%s\ncleaned_user_input=%s\nvector_query=%s\nbm25_query=%s",
        agent_name,
        cleaned_user_input or "（空）",
        vector_query or "（空）",
        bm25_query or "（空）",
    )
    relevant = search_memories(agent_name, vector_query, bm25_query)
    return f"<relevant_memories>\n{relevant}\n</relevant_memories>"
