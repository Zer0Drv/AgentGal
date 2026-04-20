"""记忆整理流程的 prompt 组装。"""

from __future__ import annotations

import re

from shared.text_utils import get_display_name
from storage.agent_files import read_agent_file
from storage.history import load_conversation_history


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
