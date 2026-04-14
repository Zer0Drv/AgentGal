"""测试 narrator 路径的回退和内容清洗。"""

import asyncio
import os
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
os.chdir(project_root)

try:
    import engine.conversation_flow as conversation_flow_module
    import engine.character as character_module
    from engine.character import Character, Narrator
    from engine.agent_schema import CharacterOutput, NarratorOutput, NarratorStatus, StateUpdaterOutput
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
async def test_call_narrator_and_route_returns_fallback_on_run_failure(monkeypatch):
    monkeypatch.setattr(
        character_module,
        "get_agent_names",
        lambda include_narrator=False: ["mitsuki"],
    )
    monkeypatch.setattr(character_module, "load_conversation_history", lambda limit=None: [])
    monkeypatch.setattr(
        character_module.Narrator,
        "_activate_next_task_if_needed",
        lambda self: None,
    )

    async def fake_run_narrator(self, *_args, **_kwargs):
        raise asyncio.TimeoutError

    monkeypatch.setattr(character_module.Narrator, "_run_narrator", fake_run_narrator)

    targets, scene_description, is_valid = await conversation_flow_module.call_narrator_and_route(
        "你好"
    )

    assert targets == []
    assert scene_description == ""
    assert is_valid is False


@pytest.mark.asyncio
async def test_call_narrator_and_route_filters_targets_and_sanitizes_scene(monkeypatch):
    monkeypatch.setattr(
        character_module,
        "get_agent_names",
        lambda include_narrator=False: ["mitsuki"],
    )
    monkeypatch.setattr(character_module, "load_conversation_history", lambda limit=None: [])
    monkeypatch.setattr(
        character_module.Narrator,
        "_activate_next_task_if_needed",
        lambda self: None,
    )
    monkeypatch.setattr(character_module, "read_agent_file", lambda *_args: "# 美月")
    monkeypatch.setattr(character_module, "get_display_name", lambda *_args: "美月")

    async def fake_run_narrator(self, *_args, **_kwargs):
        return NarratorOutput(
            targets=["mitsuki", "ghost"],
            content="场景铺垫。\n美月：这句不该由旁白说。",
        )

    monkeypatch.setattr(character_module.Narrator, "_run_narrator", fake_run_narrator)

    targets, scene_description, is_valid = await conversation_flow_module.call_narrator_and_route(
        "你好"
    )

    assert targets == ["mitsuki"]
    assert scene_description == "场景铺垫。"
    assert is_valid is True


def test_activate_next_narrator_task_moves_first_task_to_status(tmp_path, monkeypatch):
    import storage.agent_files as agent_files_module

    narrator_dir = tmp_path / "narrator"
    narrator_dir.mkdir()
    (narrator_dir / "status.md").write_text(
        "# 故事状态\n\n## 场景\n办公室\n\n## 待触发事件\n\n",
        encoding="utf-8",
    )
    (narrator_dir / "tasks.md").write_text(
        "# Narrator Tasks\n\n"
        "## 第一件事\n"
        "触发时机：现在。\n"
        "事实：门口有人敲门。\n\n"
        "## 第二件事\n"
        "触发时机：稍后。\n"
        "事实：手机亮起。\n",
        encoding="utf-8",
    )

    def fake_character_path(agent_name: str, *subpaths: str) -> str:
        return str(tmp_path / agent_name / Path(*subpaths))

    monkeypatch.setattr(character_module, "character_path", fake_character_path)
    monkeypatch.setattr(agent_files_module, "character_path", fake_character_path)

    result = Narrator()._activate_next_task_if_needed()

    assert result is not None
    assert result["operation"] == "add"
    status = (narrator_dir / "status.md").read_text(encoding="utf-8")
    assert "- [ ] 【第一件事】触发时机：现在。 事实：门口有人敲门。" in status
    tasks = (narrator_dir / "tasks.md").read_text(encoding="utf-8")
    assert "## 第一件事" not in tasks
    assert "## 第二件事" in tasks


def test_activate_next_narrator_task_keeps_queue_when_pending_event_exists(tmp_path, monkeypatch):
    import storage.agent_files as agent_files_module

    narrator_dir = tmp_path / "narrator"
    narrator_dir.mkdir()
    (narrator_dir / "status.md").write_text(
        "# 故事状态\n\n## 待触发事件\n- [ ] 【已有】当前事件\n",
        encoding="utf-8",
    )
    original_tasks = (
        "# Narrator Tasks\n\n"
        "## 第一件事\n"
        "触发时机：现在。\n"
        "事实：门口有人敲门。\n"
    )
    (narrator_dir / "tasks.md").write_text(original_tasks, encoding="utf-8")

    def fake_character_path(agent_name: str, *subpaths: str) -> str:
        return str(tmp_path / agent_name / Path(*subpaths))

    monkeypatch.setattr(character_module, "character_path", fake_character_path)
    monkeypatch.setattr(agent_files_module, "character_path", fake_character_path)

    result = Narrator()._activate_next_task_if_needed()

    assert result is None
    assert (narrator_dir / "tasks.md").read_text(encoding="utf-8") == original_tasks


@pytest.mark.asyncio
async def test_state_updater_output_writes_narrator_status_and_events(monkeypatch):
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

    await Narrator().apply_state_updates(output)

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
async def test_run_state_updater_uses_state_updater_agent(monkeypatch):
    captured: dict = {}
    applied: list[tuple] = []
    fake_agent = object()

    monkeypatch.setattr(
        conversation_flow_module,
        "read_agent_file",
        lambda agent, filename: "# narrator status\n\n## 场景\n旧场景",
    )
    monkeypatch.setattr(
        conversation_flow_module,
        "get_narrator_llm_config",
        lambda: {"model": "test-model"},
    )
    monkeypatch.setattr(
        conversation_flow_module,
        "get_state_updater_agent",
        lambda: fake_agent,
    )

    async def fake_run_structured_agent(**kwargs):
        captured.update(kwargs)
        return StateUpdaterOutput(
            status=NarratorStatus(叙事焦点="玩家私下联系角色B")
        )

    async def fake_apply_state_updates(self, output):
        applied.append((self.name, output))

    monkeypatch.setattr(conversation_flow_module, "run_structured_agent", fake_run_structured_agent)
    monkeypatch.setattr(character_module.Narrator, "apply_state_updates", fake_apply_state_updates)

    await conversation_flow_module.run_state_updater(
        "给角色B发消息",
        "手机屏幕亮了一下。",
        ["role_b"],
        [("role_b", "我看到了。")],
    )

    assert captured["agent"] is fake_agent
    assert captured["output_type"] is StateUpdaterOutput
    assert captured["usage_agent"] == "state_updater"
    assert "<current_narrator_status>" in captured["user_input"]
    assert "手机屏幕亮了一下。" in captured["user_input"]
    assert applied[0][0] == "narrator"
    assert applied[0][1].status.叙事焦点 == "玩家私下联系角色B"
