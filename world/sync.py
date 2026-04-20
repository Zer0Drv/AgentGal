"""回合结束后的世界状态同步。

职责：为出场 targets 写入 `.last_seen.json`；角色位置由 state_updater 统一维护到
`narrator/status.md` 的「角色位置」字段。
"""

from __future__ import annotations

from storage.agent_files import write_sidecar_json


_LAST_SEEN_FILE = ".last_seen.json"


def post_turn_world_sync(targets: list[str], now_time: str) -> None:
    """每轮收尾后为出场 targets 写入 .last_seen.json。"""
    if not now_time:
        return
    for agent in targets:
        write_sidecar_json(agent, _LAST_SEEN_FILE, {"last_seen": now_time})
