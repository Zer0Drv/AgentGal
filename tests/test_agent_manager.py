"""测试 narrator 路径的回退和内容清洗。"""

import asyncio
import os
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
os.chdir(project_root)

try:
    import engine.conversation_flow as conversation_flow_module
except ModuleNotFoundError as exc:
    pytest.skip(f"skip conversation flow tests: missing dependency ({exc})", allow_module_level=True)


def test_sanitize_narrator_scene_description_truncates_character_dialogue(monkeypatch):
    monkeypatch.setattr(
        conversation_flow_module,
        "get_agent_names",
        lambda include_narrator=False: ["mitsuki"],
    )
    monkeypatch.setattr(conversation_flow_module, "read_agent_file", lambda *_args: "# 美月")
    monkeypatch.setattr(conversation_flow_module, "get_display_name", lambda *_args: "美月")

    scene = "房间里安静下来。\n美月：这句不该由旁白说。\n她向前走了一步。"
    sanitized = conversation_flow_module._sanitize_narrator_scene_description(scene)

    assert sanitized == "房间里安静下来。"


@pytest.mark.asyncio
async def test_call_narrator_and_route_returns_fallback_on_run_failure(monkeypatch):
    monkeypatch.setattr(
        conversation_flow_module,
        "get_agent_names",
        lambda include_narrator=False: ["mitsuki"],
    )
    monkeypatch.setattr(conversation_flow_module, "load_conversation_history", lambda limit=None: [])

    async def fake_run_agent(*_args, **_kwargs):
        raise asyncio.TimeoutError

    monkeypatch.setattr(conversation_flow_module, "_run_conversation_agent", fake_run_agent)

    targets, scene_description, is_valid = await conversation_flow_module.call_narrator_and_route(
        "你好"
    )

    assert targets == []
    assert scene_description == ""
    assert is_valid is False


@pytest.mark.asyncio
async def test_call_narrator_and_route_filters_targets_and_sanitizes_scene(monkeypatch):
    monkeypatch.setattr(
        conversation_flow_module,
        "get_agent_names",
        lambda include_narrator=False: ["mitsuki"],
    )
    monkeypatch.setattr(conversation_flow_module, "load_conversation_history", lambda limit=None: [])
    monkeypatch.setattr(conversation_flow_module, "read_agent_file", lambda *_args: "# 美月")
    monkeypatch.setattr(conversation_flow_module, "get_display_name", lambda *_args: "美月")

    async def fake_run_agent(*_args, **_kwargs):
        return conversation_flow_module.NarratorOutput(
            targets=["mitsuki", "ghost"],
            content="场景铺垫。\n美月：这句不该由旁白说。",
        )

    monkeypatch.setattr(conversation_flow_module, "_run_conversation_agent", fake_run_agent)

    targets, scene_description, is_valid = await conversation_flow_module.call_narrator_and_route(
        "你好"
    )

    assert targets == ["mitsuki"]
    assert scene_description == "场景铺垫。"
    assert is_valid is True
