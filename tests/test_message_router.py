"""测试 raw history 的 turn 分配规则。"""

import os
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
os.chdir(project_root)

import storage.message_router as router_module
from storage.message_router import MessageRouter


@pytest.mark.asyncio
async def test_narrator_response_starts_next_turn(monkeypatch):
    current_turn = 5
    written: list[dict] = []

    def fake_read_turn_counter() -> int:
        return current_turn

    def fake_increment_turn_counter() -> int:
        nonlocal current_turn
        current_turn += 1
        return current_turn

    async def fake_append_message(message: dict) -> None:
        written.append(dict(message))

    monkeypatch.setattr(router_module, "get_agent_names", lambda: ["mitsuki", "narrator"])
    monkeypatch.setattr(router_module, "read_turn_counter", fake_read_turn_counter)
    monkeypatch.setattr(router_module, "increment_turn_counter", fake_increment_turn_counter)
    monkeypatch.setattr(router_module, "append_message", fake_append_message)

    router = MessageRouter()

    player_turn = await router.broadcast_player_message(["mitsuki"], "上一段旁白后的回应")
    narrator_turn = await router.broadcast_agent_response("narrator", ["mitsuki"], "新的场景")
    character_turn = await router.broadcast_agent_response("mitsuki", ["mitsuki"], "角色回应")

    assert player_turn == 5
    assert narrator_turn == 6
    assert character_turn == 6
    assert [(m["role"], m["turn"]) for m in written] == [
        ("player", 5),
        ("narrator", 6),
        ("mitsuki", 6),
    ]
    assert written[0]["visible_to"] == ["mitsuki", "narrator"]
