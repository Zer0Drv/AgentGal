"""测试 narrator 路径的回退和内容清洗。"""

import asyncio
import os
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
os.chdir(project_root)

try:
    import engine.character as character_module
    from engine.character import Character, Narrator
    from agents.schema import CharacterOutput, NarratorOutput, NarratorStatus, StateUpdaterOutput
except ModuleNotFoundError as exc:
    pytest.skip(f"skip conversation flow tests: missing dependency ({exc})", allow_module_level=True)


def test_sanitize_narrator_scene_description_truncates_character_dialogue(monkeypatch):
    monkeypatch.setattr(
        character_module,
        "get_agent_names",
        lambda include_narrator=False: ["mitsuki"],
    )
    monkeypatch.setattr(character_module, "read_agent_file", lambda *_args: "# 美月")
    monkeypatch.setattr(character_module, "get_display_name", lambda *_args: "美月")

    scene = "房间里安静下来。\n美月：这句不该由旁白说。\n她向前走了一步。"
    sanitized = Narrator()._sanitize_scene_description(scene)

    assert sanitized == "房间里安静下来。"


@pytest.mark.asyncio
async def test_narrator_route_returns_fallback_on_run_failure(monkeypatch):
    monkeypatch.setattr(
        character_module,
        "get_agent_names",
        lambda include_narrator=False: ["mitsuki"],
    )
    monkeypatch.setattr(character_module, "load_conversation_history", lambda limit=None: [])

    async def fake_run_narrator(self, *_args, **_kwargs):
        raise asyncio.TimeoutError

    monkeypatch.setattr(character_module.Narrator, "_run_narrator", fake_run_narrator)

    targets, scene_description, new_characters, is_valid = await Narrator().route("你好")

    assert targets == []
    assert scene_description == ""
    assert new_characters == []
    assert is_valid is False


@pytest.mark.asyncio
async def test_narrator_route_filters_targets_and_sanitizes_scene(monkeypatch):
    monkeypatch.setattr(
        character_module,
        "get_agent_names",
        lambda include_narrator=False: ["mitsuki"],
    )
    monkeypatch.setattr(character_module, "load_conversation_history", lambda limit=None: [])
    monkeypatch.setattr(character_module, "read_agent_file", lambda *_args: "# 美月")
    monkeypatch.setattr(character_module, "get_display_name", lambda *_args: "美月")

    async def fake_run_narrator(self, *_args, **_kwargs):
        return NarratorOutput(
            targets=["mitsuki", "ghost"],
            content="场景铺垫。\n美月：这句不该由旁白说。",
        )

    monkeypatch.setattr(character_module.Narrator, "_run_narrator", fake_run_narrator)

    targets, scene_description, new_characters, is_valid = await Narrator().route("你好")

    assert targets == ["mitsuki"]
    assert scene_description == "场景铺垫。"
    assert is_valid is True


@pytest.mark.asyncio
async def test_narrator_route_retries_when_targets_filter_to_empty(monkeypatch):
    monkeypatch.setattr(
        character_module,
        "get_agent_names",
        lambda include_narrator=False: ["mitsuki"],
    )
    monkeypatch.setattr(character_module, "load_conversation_history", lambda limit=None: [])
    calls = 0

    async def fake_run_narrator(self, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return NarratorOutput(targets=["ghost"], content="走廊里传来广播声。")
        return NarratorOutput(targets=["mitsuki"], content="美月站在走廊尽头。")

    monkeypatch.setattr(character_module.Narrator, "_run_narrator", fake_run_narrator)

    targets, scene_description, new_characters, is_valid = await Narrator().route("回家睡觉")

    assert calls == 2
    assert targets == ["mitsuki"]
    assert scene_description == "美月站在走廊尽头。"
    assert is_valid is True


@pytest.mark.asyncio
async def test_narrator_route_retries_when_targets_are_empty(monkeypatch):
    monkeypatch.setattr(
        character_module,
        "get_agent_names",
        lambda include_narrator=False: ["mitsuki"],
    )
    monkeypatch.setattr(character_module, "load_conversation_history", lambda limit=None: [])
    calls = 0

    async def fake_run_narrator(self, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return NarratorOutput(targets=[], content="走廊里传来广播声。")
        return NarratorOutput(targets=["mitsuki"], content="美月站在走廊尽头。")

    monkeypatch.setattr(character_module.Narrator, "_run_narrator", fake_run_narrator)

    targets, scene_description, new_characters, is_valid = await Narrator().route("回家睡觉")

    assert calls == 2
    assert targets == ["mitsuki"]
    assert scene_description == "美月站在走廊尽头。"
    assert is_valid is True


@pytest.mark.asyncio
async def test_narrator_route_allows_spawn_without_existing_targets(monkeypatch):
    monkeypatch.setattr(
        character_module,
        "get_agent_names",
        lambda include_narrator=False: ["mitsuki"],
    )
    monkeypatch.setattr(character_module, "load_conversation_history", lambda limit=None: [])
    calls = 0

    async def fake_run_narrator(self, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        return NarratorOutput(
            targets=[],
            content="门外有人停下脚步。",
            new_characters=[
                {
                    "display_name": "桥本志津",
                    "relation_to": "mitsuki",
                    "relation_description": "美月的妈妈",
                }
            ],
        )

    monkeypatch.setattr(character_module.Narrator, "_run_narrator", fake_run_narrator)

    targets, scene_description, new_characters, is_valid = await Narrator().route("回家睡觉")

    assert calls == 1
    assert targets == []
    assert len(new_characters) == 1
    assert new_characters[0].display_name == "桥本志津"
    assert scene_description == "门外有人停下脚步。"
    assert is_valid is True


@pytest.mark.asyncio
async def test_narrator_route_rejects_scene_without_valid_targets(monkeypatch):
    monkeypatch.setattr(
        character_module,
        "get_agent_names",
        lambda include_narrator=False: ["mitsuki"],
    )
    monkeypatch.setattr(character_module, "load_conversation_history", lambda limit=None: [])

    async def fake_run_narrator(self, *_args, **_kwargs):
        return NarratorOutput(targets=["ghost"], content="走廊里传来广播声。")

    monkeypatch.setattr(character_module.Narrator, "_run_narrator", fake_run_narrator)

    targets, scene_description, new_characters, is_valid = await Narrator().route("回家睡觉")

    assert targets == []
    assert scene_description == "走廊里传来广播声。"
    assert is_valid is False


def test_state_updater_output_writes_narrator_status_and_events(monkeypatch):
    calls: list[tuple[str, str, str, str | None]] = []

    monkeypatch.setattr(
        character_module,
        "update_status",
        lambda agent, field, content: calls.append(("status", agent, field, content)) or {},
    )
    monkeypatch.setattr(
        character_module,
        "mark_event_triggered",
        lambda agent, event, section: calls.append(("triggered", agent, event, section)) or {},
    )
    monkeypatch.setattr(
        character_module,
        "add_pending_event",
        lambda agent, event, section: calls.append(("add_event", agent, event, section)) or {},
    )

    output = StateUpdaterOutput(
        status=NarratorStatus(
            场景="餐厅",
            角色位置="- 玩家：餐桌旁",
            当前时间="10月24日 08:40",
        ),
        triggered=["角色B来电"],
        add_event=["【楼下碰面】10月24日 09:30 角色B到达公寓楼下"],
    )

    Narrator()._apply_state_updates(output)

    assert ("status", "narrator", "场景", "餐厅") in calls
    assert ("status", "narrator", "角色位置", "- 玩家：餐桌旁") in calls
    assert ("status", "narrator", "当前时间", "10月24日 08:40") in calls
    assert ("triggered", "narrator", "角色B来电", "待触发事件") in calls
    assert (
        "add_event",
        "narrator",
        "【楼下碰面】10月24日 09:30 角色B到达公寓楼下",
        "待触发事件",
    ) in calls


@pytest.mark.asyncio
async def test_apply_response_updates_logs_structured_file_updates(monkeypatch):
    logs: list[tuple[tuple, dict]] = []

    monkeypatch.setattr(
        character_module,
        "update_memory",
        lambda agent, content: {
            "file": "memory.md",
            "target": "长期记忆",
            "operation": "append",
            "appended": content,
        },
    )
    monkeypatch.setattr(
        character_module,
        "update_status",
        lambda agent, field, content: {
            "file": "status.md",
            "target": field,
            "operation": "replace",
            "before": "旧场景",
            "after": content,
        },
    )
    monkeypatch.setattr(
        character_module,
        "update_player",
        lambda agent, field, content: {
            "file": "tmp_user.md",
            "target": field,
            "operation": "append",
            "appended": content,
        },
    )
    monkeypatch.setattr(
        character_module,
        "mark_event_triggered",
        lambda agent, event, section: {
            "file": "status.md",
            "target": section,
            "operation": "remove",
            "removed": f"- [ ] 【{event}】去天台",
        },
    )
    monkeypatch.setattr(
        character_module,
        "add_pending_event",
        lambda agent, event, section: (
            {
                "file": "status.md",
                "target": section,
                "operation": "skip",
                "reason": "【重复】已存在，跳过",
            }
            if "重复" in event
            else {
                "file": "status.md",
                "target": section,
                "operation": "add",
                "added": f"- [ ] {event}",
            }
        ),
    )

    def fake_log_debug(*args, **kwargs):
        logs.append((args, kwargs))

    monkeypatch.setattr(character_module.routing_logger, "debug", fake_log_debug)

    output = CharacterOutput(
        content="回应",
        memory=(
            "- **时间**：10月24日 上午\n"
            "- **地点**：图书馆\n"
            "- **在场**：我、玩家\n"
            "- **内容**：玩家主动替我解围。"
        ),
        status={"场景": "图书馆二楼靠窗座位"},
        player={"对方是什么人": "- 很直接\n- 会主动推进话题"},
        triggered=["去天台"],
        add_event=["【新计划】去图书馆", "【重复】去图书馆"],
    )

    await Character("lilith")._apply_updates(output)

    args, kwargs = logs[0]
    assert args == ("[FileUpdate] 文件更新: agent=%s, count=%s", "lilith", 6)
    extra = kwargs["extra"]
    assert extra["event.name"] == "agentgal.routing.file_updates"
    assert extra["file_update.agent"] == "lilith"
    assert extra["file_update.count"] == 6
    assert "file_update.items" not in extra
    assert extra["file_update.updates"] == [
        {
            "file": "memory.md",
            "target": "长期记忆",
            "operation": "append",
            "appended": output.memory,
        },
        {
            "file": "status.md",
            "target": "场景",
            "operation": "replace",
            "before": "旧场景",
            "after": "图书馆二楼靠窗座位",
        },
        {
            "file": "tmp_user.md",
            "target": "对方是什么人",
            "operation": "append",
            "appended": "- 很直接\n- 会主动推进话题",
        },
        {
            "file": "status.md",
            "target": "打算",
            "operation": "remove",
            "removed": "- [ ] 【去天台】去天台",
        },
        {
            "file": "status.md",
            "target": "打算",
            "operation": "add",
            "added": "- [ ] 【新计划】去图书馆",
        },
        {
            "file": "status.md",
            "target": "打算",
            "operation": "skip",
            "reason": "【重复】已存在，跳过",
        },
    ]


@pytest.mark.asyncio
async def test_narrator_update_state_uses_state_updater_agent(monkeypatch):
    captured: dict = {}
    applied: list[tuple] = []
    fake_agent = object()

    def fake_read_agent_file(agent, filename):
        files = {
            ("narrator", "status.md"): "# narrator status\n\n## 场景\n旧场景",
            ("role_b", "status.md"): "# 角色B的状态\n\n## 打算\n- [ ] 【楼下碰面】10月24日 09:30 公寓楼下。去见玩家。",
            ("role_b", "soul.md"): "# 角色B",
        }
        return files.get((agent, filename), "")

    monkeypatch.setattr(character_module, "read_agent_file", fake_read_agent_file)
    monkeypatch.setattr(
        character_module,
        "get_agent_names",
        lambda include_narrator=False: ["role_b"],
    )
    monkeypatch.setattr(character_module, "get_display_name", lambda *_args: "角色B")
    monkeypatch.setattr(
        character_module,
        "get_narrator_llm_config",
        lambda: {"model": "test-model"},
    )
    monkeypatch.setattr(
        character_module,
        "get_state_updater_agent",
        lambda: fake_agent,
    )
    monkeypatch.setattr(character_module, "build_schedule_snapshot", lambda _t: "")
    monkeypatch.setattr(character_module, "post_turn_world_sync", lambda *_a, **_k: None)
    history_limits: list[int | None] = []

    def fake_load_conversation_history(limit=None):
        history_limits.append(limit)
        messages = [
            {"role": "player", "content": "更早的问题"},
            {"role": "narrator", "content": "手机在掌心震了一下。"},
            {"role": "player", "content": "送到门口会被家人看到吗？"},
            {"role": "role_b", "content": "应该不会，家里人还没回来。"},
        ]
        return messages if limit is None else messages[-limit:]

    monkeypatch.setattr(
        character_module,
        "load_conversation_history",
        fake_load_conversation_history,
    )

    async def fake_run_structured_agent(**kwargs):
        captured.update(kwargs)
        return StateUpdaterOutput(
            status=NarratorStatus(叙事焦点="玩家私下联系角色B")
        )

    def fake_apply_state_updates(self, output):
        applied.append((self.name, output))

    monkeypatch.setattr(character_module, "run_structured_agent", fake_run_structured_agent)
    monkeypatch.setattr(character_module.Narrator, "_apply_state_updates", fake_apply_state_updates)

    await Narrator().update_state([])

    assert captured["agent"] is fake_agent
    assert captured["output_type"] is StateUpdaterOutput
    assert captured["usage_agent"] == "state_updater"
    assert history_limits == [3]
    user_input = captured["user_input"]
    assert user_input.index("<character_intention>") < user_input.index("<current_narrator_status>")
    assert user_input.index("<current_narrator_status>") < user_input.index("<recent_history>")
    assert "玩家: 更早的问题" not in user_input
    assert "旁白: 手机在掌心震了一下。" in user_input
    assert "玩家: 送到门口会被家人看到吗？" in user_input
    assert "role_b: 应该不会，家里人还没回来。" in user_input
    assert "【role_b / 角色B】" in user_input
    assert "【楼下碰面】10月24日 09:30 公寓楼下。去见玩家。" in user_input
    assert "<character_intentions>" not in user_input
    assert "<player_input>" not in user_input
    assert "<narrator_targets>" not in user_input
    assert "<narrator_content>" not in user_input
    assert "<agent_responses>" not in user_input
    assert "<milestones>" not in user_input
    assert "给角色B发消息" not in user_input
    assert "手机屏幕亮了一下。" not in user_input
    assert "我看到了。" not in user_input
    assert applied[0][0] == "narrator"
    assert applied[0][1].status.叙事焦点 == "玩家私下联系角色B"
