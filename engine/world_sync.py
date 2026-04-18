"""回合结束后的世界状态同步。

职责：
- targets 刚在场景里 → 同步 当前位置 = scene，并更新 .last_seen.json
- 时段切换时，非 targets 且 打算 为空的角色 → 按 schedule 刷默认 当前位置
- 非空 打算 的角色在时段切换时保留原位置（他们还在做自己的事）
"""

from __future__ import annotations

from engine.world_schedule import get_default_location, parse_game_time
from memory.parser import extract_status_field
from shared.config import get_agent_names
from storage.agent_files import read_agent_file, update_status, write_sidecar_json


_LAST_SEEN_FILE = ".last_seen.json"


def post_turn_world_sync(
    scene: str,
    targets: list[str],
    prev_time: str,
    now_time: str,
) -> None:
    """每轮收尾后同步 当前位置 与 .last_seen.json。"""
    for agent in targets:
        if scene:
            update_status(agent, "当前位置", scene)
        if now_time:
            write_sidecar_json(agent, _LAST_SEEN_FILE, {"last_seen": now_time})

    prev_slot = parse_game_time(prev_time)
    now_slot = parse_game_time(now_time)
    if now_slot is None or prev_slot == now_slot:
        return

    targets_set = set(targets)
    for agent in get_agent_names(include_narrator=False):
        if agent in targets_set:
            continue
        status_text = read_agent_file(agent, "status.md")
        if extract_status_field(status_text, "打算").strip():
            continue
        default_loc = get_default_location(agent, now_slot, now_time)
        if not default_loc:
            continue
        update_status(agent, "当前位置", default_loc)
