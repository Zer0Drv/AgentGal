"""所有 Agent 的 prompt 构建（对话 + 记忆整理）。"""

from __future__ import annotations

import re
from pathlib import Path

from memory.parser import extract_status_field
from shared.config import (
    HISTORY_HIGH,
    HISTORY_LOW,
    PROJECT_ROOT,
    character_path,
    get_agent_names,
)
from shared.text_utils import get_display_name
from storage.agent_files import get_allowed_fields, read_agent_file
from storage.agent_files import read_sidecar_json, write_sidecar_json
from storage.history import load_conversation_history  # re-export for callers
from log_config.routing import routing_logger


# ---------------------------------------------------------------------------
# 对话历史窗口（原 engine/history.py）
# ---------------------------------------------------------------------------


def _apply_high_low_watermark(
    agent_name: str,
    visible_indices: list[int],
    start_raw_index: int,
    high: int,
    low: int,
) -> tuple[int, list[int], bool]:
    """应用高低水位缓冲，返回本轮应显示的消息下标范围及是否触发了截断。"""
    if not visible_indices:
        return 0, [], False

    kept_indices = [idx for idx in visible_indices if idx >= start_raw_index]
    if not kept_indices:
        start_raw_index = visible_indices[0]
        kept_indices = visible_indices

    was_truncated = False
    if len(kept_indices) > high:
        kept_indices = kept_indices[-low:]
        start_raw_index = kept_indices[0]
        was_truncated = True

    return start_raw_index, kept_indices, was_truncated


def _get_windowed_visible_messages(
    agent_name: str, raw_messages: list[dict]
) -> tuple[list[dict], bool]:
    """按 agent 维度应用高低水位窗口，并持久化窗口起点。返回 (消息列表, 是否触发截断)。"""
    if not raw_messages:
        return [], False

    if agent_name != "narrator":
        visible_indices = [
            idx for idx, msg in enumerate(raw_messages) if agent_name in msg.get("visible_to", [])
        ]
    else:
        visible_indices = list(range(len(raw_messages)))

    if not visible_indices:
        return [], False

    _hw = read_sidecar_json(agent_name, ".history_window_state.json")
    start_raw_index = min(max(0, int(_hw.get("start_raw_index", 0))), len(raw_messages) - 1)
    next_start_raw_index, kept_indices, was_truncated = _apply_high_low_watermark(
        agent_name,
        visible_indices,
        start_raw_index,
        HISTORY_HIGH,
        HISTORY_LOW,
    )
    write_sidecar_json(
        agent_name,
        ".history_window_state.json",
        {"start_raw_index": max(0, next_start_raw_index)},
    )
    return [raw_messages[idx] for idx in kept_indices], was_truncated


def build_history_transcript(
    agent_name: str,
    raw_messages: list[dict],
) -> tuple[str, bool]:
    """将 JSONL 原始消息转为单段历史文本（高低水位窗口 + 可见性过滤）。返回 (文本, 是否触发截断)。"""
    visible, was_truncated = _get_windowed_visible_messages(agent_name, raw_messages)

    if not visible:
        return "", False

    lines: list[str] = []
    for msg in visible:
        role = msg.get("role", "unknown")
        content = re.sub(r"\n+", "\n", msg.get("content", "").strip())
        if not content:
            continue
        speaker = "玩家" if role == "player" else role
        lines.append(f"{speaker}: {content}")

    return "\n\n".join(lines), was_truncated


# ---------------------------------------------------------------------------
# 对话 prompt 构建
# ---------------------------------------------------------------------------


def build_search_query(agent_name: str, user_input: str) -> str:
    """用叙事焦点富化检索 query。"""
    if agent_name == "narrator":
        return user_input
    try:
        status = Path(character_path("narrator", "status.md")).read_text(encoding="utf-8")
        focus = extract_status_field(status, "叙事焦点").strip()
    except OSError:
        focus = ""
    if not focus:
        return user_input
    return f"{focus}\n{user_input}"


def build_system_prompt(agent_name: str, soul_content: str) -> str:
    """构建 system prompt（仅包含稳定的身份与规则部分）。"""
    prompt_name = "narrator_prompt.txt" if agent_name == "narrator" else "character_prompt.txt"
    prompt_template = (PROJECT_ROOT / "prompts" / prompt_name).read_text(encoding="utf-8")
    excluded_status_fields = {"打算"} if agent_name != "narrator" else set()
    status_fields = "、".join(
        field
        for field in get_allowed_fields(agent_name, "status")
        if field not in excluded_status_fields
    )
    player_fields = (
        "、".join(get_allowed_fields(agent_name, "user")) if agent_name != "narrator" else ""
    )
    display_name = get_display_name(agent_name, soul_content)

    characters = get_agent_names(include_narrator=False)
    characters_scene_list = "\n".join(
        f"- {get_display_name(character, read_agent_file(character, 'soul.md'))}：[位置] 或 不在场"
        for character in characters
    )
    valid_targets = ", ".join(characters)

    return prompt_template.format(
        agent_name=agent_name,
        display_name=display_name,
        soul=soul_content,
        status_fields=status_fields,
        player_fields=player_fields,
        characters_scene_list=characters_scene_list,
        valid_targets=valid_targets,
    )


def build_user_message(
    agent_name: str,
    latest_user_input: str,
    memory_prefix: str,
    raw_messages: list[dict] | None = None,
) -> tuple[str, bool]:
    """构建单条大 user message，按稳定度排序上下文。返回 (消息文本, 是否触发历史截断)。"""
    parts: list[str] = []

    growth_content = read_agent_file(agent_name, "growth.md") if agent_name != "narrator" else ""
    user_content = read_agent_file(agent_name, "user.md") if agent_name != "narrator" else ""
    history, was_truncated = build_history_transcript(agent_name, raw_messages or [])
    status_content = read_agent_file(agent_name, "status.md")

    parts.append(f"<growth>\n{growth_content.strip()}\n</growth>" if growth_content else "")
    parts.append(f"最近对话历史:\n\n{history}" if history else "")
    parts.append(
        f"<user_profile>\n{user_content.strip()}\n</user_profile>" if user_content else ""
    )
    parts.append(f"<status>\n{status_content}\n</status>" if status_content else "")
    parts.append(memory_prefix if memory_prefix else "")
    parts.append(f"玩家新消息: {latest_user_input}")

    return "\n\n---\n\n".join(part for part in parts if part), was_truncated


# ---------------------------------------------------------------------------
# 记忆整理 prompt 构建（原 memory/consolidation_inputs.py）
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^\s*##\s*([^\n]+)\n?")


def _get_owner_display_name(agent_name: str) -> str:
    """读取当前整理对象的显示名，失败时回退到 agent_name。"""
    soul_content = read_agent_file(agent_name, "soul.md") or ""
    return get_display_name(agent_name, soul_content)


def _extract_heading_name(content: str) -> str | None:
    """从角色消息首行的 ## 标题里提取名字。"""
    match = _HEADING_RE.match(content or "")
    if not match:
        return None
    name = match.group(1).strip()
    return name or None


def _strip_leading_character_heading(content: str) -> str:
    """移除消息开头的角色标题，避免出现"我：## 陈晓"。"""
    match = _HEADING_RE.match(content or "")
    if not match:
        return (content or "").strip()
    return (content[match.end():]).lstrip("\n").strip()


def _format_message_for_owner(agent_name: str, msg: dict) -> str:
    role = msg.get("role", "unknown")
    raw_content = (msg.get("content") or "").strip()
    if not raw_content:
        return ""

    content = _strip_leading_character_heading(raw_content)

    if role == "narrator":
        speaker = "旁白"
    elif role == "player":
        speaker = "玩家" if agent_name == "narrator" else "他"
    elif agent_name != "narrator" and role == agent_name:
        speaker = "我"
    else:
        speaker = _extract_heading_name(raw_content) or role

    if not content:
        return ""
    return f"{speaker}：{content}"


def build_memory_owner_block(agent_name: str) -> str:
    """构造 memory merge 的记忆主体约束块。"""
    display_name = _get_owner_display_name(agent_name)
    no_self_ref = f"\u201c{display_name}\u201d\u6216\u201c{display_name}\uff08\u6211\uff09\u201d"
    lines = [
        "<memory_owner>",
        f"当前整理对象：{display_name}（agent_name={agent_name}）",
        f"最终输出：{display_name} 的长期记忆",
        "代词约定：",
        f"- 我 = {display_name}",
        "- 他 = 玩家",
        "- 其他角色保留实名",
        "- 旁白 = narrator 的场景描述，提供场景信息",
        "- 旁白中的\u201c你 / 你家 / 你这边\u201d默认指玩家一侧，不指当前整理对象",
        "写作要求：",
        "- 最终输出始终写成当前整理对象的记忆",
        f"- 用\u201c我\u201d指代 {display_name}",
        "- 用\u201c他\u201d指代玩家",
        "- 其他角色直接使用实名",
        "- 用 raw_dialogue 校正事实、顺序和台词",
        "- 输出前先把输入材料中的视角转换成当前整理对象视角，再写入时间 / 地点 / 在场 / 内容",
        f"- 不要把当前整理对象再写成{no_self_ref}",
        "- 不要保留\u201c玩家\u201d这种材料标签；要改写成当前视角下自然成立的称呼",
        "</memory_owner>",
    ]
    return "\n".join(lines)


def format_raw_dialogue_for_owner(agent_name: str, limit: int) -> str:
    """读取最近原始消息，过滤并转换成适合当前整理对象阅读的视角文本。"""
    raw_messages = load_conversation_history(limit=limit)
    raw_messages = [m for m in raw_messages if agent_name in m.get("visible_to", [])]

    formatted = [
        line
        for line in (_format_message_for_owner(agent_name, msg) for msg in raw_messages)
        if line
    ]
    return "\n\n".join(formatted)


def build_memory_merge_payload(
    agent_name: str,
    memory_entries: str,
    raw_dialogue: str = "",
) -> str:
    """构造 memory merge 的 user payload。"""
    payload_parts = [
        build_memory_owner_block(agent_name),
        f"<memory_entries>\n{memory_entries}\n</memory_entries>",
    ]
    if raw_dialogue:
        payload_parts.append(f"<raw_dialogue>\n{raw_dialogue}\n</raw_dialogue>")
    return "\n\n".join(payload_parts)
