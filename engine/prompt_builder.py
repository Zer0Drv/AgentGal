"""对话运行时的 prompt 构建（角色 / narrator user message、history 窗口、schedule 快照）。"""

from __future__ import annotations

import re
from pathlib import Path

from world.schedule import (
    get_default_location,
    load_character_schedule,
    parse_game_time,
)
from memory.parser import extract_status_field
from prompts.runtime_prompts import CHARACTER, NARRATOR
from shared.config import (
    HISTORY_HIGH,
    HISTORY_LOW,
    character_path,
    get_agent_names,
)
from shared.text_utils import extract_identity, get_display_name, role_to_speaker
from storage.agent_files import (
    get_allowed_fields,
    read_agent_file,
    read_sidecar_json,
    write_sidecar_json,
)
from log_config.routing import routing_logger


_WEEKDAY_EN_TO_CN = {
    "mon": "一",
    "tue": "二",
    "wed": "三",
    "thu": "四",
    "fri": "五",
    "sat": "六",
    "sun": "日",
}


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
    *,
    inject_turn_markers: bool = False,
) -> tuple[str, bool]:
    """将 JSONL 原始消息转为历史文本，但只保留最后一条可见旁白。

    高低水位窗口与可见性过滤仍照常应用。
    inject_turn_markers=True 时在每条前加 `[turn=N]` 前缀，供 detector / 整理输入使用。
    返回 (文本, 是否触发截断)。
    """
    visible, was_truncated = _get_windowed_visible_messages(agent_name, raw_messages)

    if not visible:
        return "", False

    last_narrator_index: int | None = None
    for idx in range(len(visible) - 1, -1, -1):
        msg = visible[idx]
        if msg.get("role") != "narrator":
            continue
        content = re.sub(r"\n+", "\n", msg.get("content", "").strip())
        if not content:
            continue
        last_narrator_index = idx
        break

    lines: list[str] = []
    for idx, msg in enumerate(visible):
        role = msg.get("role", "unknown")
        if role == "narrator" and idx != last_narrator_index:
            continue
        content = re.sub(r"\n+", "\n", msg.get("content", "").strip())
        if not content:
            continue
        prefix = ""
        if inject_turn_markers:
            turn = msg.get("turn")
            if isinstance(turn, int) and turn > 0:
                prefix = f"[turn={turn}] "
        lines.append(f"{prefix}{role_to_speaker(role)}: {content}")

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
    if agent_name == "narrator":
        return NARRATOR.format(soul=soul_content)

    excluded_status_fields = {"打算"}
    status_fields = "、".join(
        field
        for field in get_allowed_fields(agent_name, "status")
        if field not in excluded_status_fields
    )
    player_fields = "、".join(get_allowed_fields(agent_name, "user"))
    display_name = get_display_name(agent_name, soul_content)
    relation_targets: list[str] = []
    seen_targets: set[str] = set()
    for target_name in get_agent_names(include_narrator=False):
        if target_name == agent_name:
            continue
        target_soul = read_agent_file(target_name, "soul.md")
        target_display = get_display_name(target_name, target_soul)
        if target_display in seen_targets:
            continue
        seen_targets.add(target_display)
        relation_targets.append(target_display)
    valid_targets = ", ".join(relation_targets)

    return CHARACTER.format(
        agent_name=agent_name,
        display_name=display_name,
        soul=soul_content,
        status_fields=status_fields,
        player_fields=player_fields,
        valid_targets=valid_targets,
    )


def _format_days(days: list[str]) -> str:
    """mon/tue/... 折叠成中文周几表达。"""
    unique = set(days)
    if unique == {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}:
        return "每天"
    if unique == {"mon", "tue", "wed", "thu", "fri"}:
        return "周一至周五"
    if unique == {"sat", "sun"}:
        return "周末"
    ordered = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    return "、".join(f"周{_WEEKDAY_EN_TO_CN[d]}" for d in ordered if d in unique)


def build_my_schedule_block(agent_name: str) -> str:
    """<my_schedule>：character 专用，渲染自己的 schedule.json。"""
    if agent_name == "narrator":
        return ""
    schedule = load_character_schedule(agent_name)
    if not schedule.periods:
        return ""

    blocks: list[str] = []
    for period in schedule.periods:
        header = period.name or "日程"
        period_lines = [f"{header}（{period.start} 至 {period.end}）"]
        for slot in period.slots:
            period_lines.append(f"- {_format_days(slot.days)} {slot.time}：{slot.location}")
        blocks.append("\n".join(period_lines))

    body = "\n\n".join(blocks)
    return f"<my_schedule>\n{body}\n</my_schedule>"


def build_schedule_snapshot(game_time: str) -> str:
    """渲染 state_updater 输入的 <schedule_snapshot>：当前时段各角色按 schedule 的默认位置。

    未配置 schedule 或 game_time 解析不出 slot 的角色标注为「（无日程）」，供 LLM 判断。
    无任何主要角色时返回空字符串。
    """
    slot_key = parse_game_time(game_time) if game_time else None
    rows: list[str] = []
    for agent in get_agent_names(include_narrator=False):
        soul = read_agent_file(agent, "soul.md")
        display = get_display_name(agent, soul) if soul else agent
        location = ""
        if slot_key is not None:
            location = (get_default_location(agent, slot_key, game_time) or "").strip()
        rows.append(f"- {display}：{location}" if location else f"- {display}：（无日程）")
    if not rows:
        return ""
    header = (
        f'<schedule_snapshot game_time="{game_time}">' if game_time else "<schedule_snapshot>"
    )
    return f"{header}\n" + "\n".join(rows) + "\n</schedule_snapshot>"


def build_fields_block(agent_name: str) -> str:
    """narrator 专用：列出当前所有候选角色的 id / 显示名 / identity。"""
    if agent_name != "narrator":
        return ""
    rows: list[str] = []
    for name in get_agent_names(include_narrator=False):
        soul = read_agent_file(name, "soul.md")
        display = get_display_name(name, soul)
        identity = extract_identity(soul)
        suffix = f"｜{identity}" if identity else ""
        rows.append(f"- {name}: {display}{suffix}")
    if not rows:
        return ""
    return "<fields>\n" + "\n".join(rows) + "\n</fields>"


def build_relations_block(audience: str) -> str:
    """character 专用：直接把 audience 的 relations.md 整块注入 <relations>。"""
    if audience == "narrator":
        return ""
    content = read_agent_file(audience, "relations.md").strip()
    if not content:
        return ""
    return f"<relations>\n{content}\n</relations>"


def build_user_message(
    agent_name: str,
    latest_user_input: str,
    memory_prefix: str,
    raw_messages: list[dict] | None = None,
) -> tuple[str, bool]:
    """构建单条大 user message，按稳定度排序上下文。返回 (消息文本, 是否触发历史截断)。"""
    parts: list[str] = []

    is_narrator = agent_name == "narrator"
    growth_content = read_agent_file(agent_name, "growth.md") if not is_narrator else ""
    user_content = read_agent_file(agent_name, "user.md") if not is_narrator else ""
    history, was_truncated = build_history_transcript(agent_name, raw_messages or [])
    status_content = read_agent_file(agent_name, "status.md")
    my_schedule = build_my_schedule_block(agent_name) if not is_narrator else ""
    relations = "" if is_narrator else build_relations_block(agent_name)

    parts.append(my_schedule)
    parts.append(f"<growth>\n{growth_content.strip()}\n</growth>" if growth_content else "")
    parts.append(
        f"<user_profile>\n{user_content.strip()}\n</user_profile>" if user_content else ""
    )
    parts.append(build_fields_block(agent_name))
    parts.append(f"最近对话历史:\n\n{history}" if history else "")
    parts.append(
        f"<status>\n{status_content.strip()}\n</status>" if status_content.strip() else ""
    )
    parts.append(relations)
    parts.append(memory_prefix if memory_prefix else "")
    parts.append(f"玩家新消息: {latest_user_input}")

    return "\n\n---\n\n".join(part for part in parts if part), was_truncated
