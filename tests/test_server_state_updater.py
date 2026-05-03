"""测试 server 层 state_updater 后台任务协调。"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

project_root = Path(__file__).parent.parent
os.chdir(project_root)

try:
    import server as server_module
except ModuleNotFoundError as exc:
    pytest.skip(f"skip server tests: missing dependency ({exc})", allow_module_level=True)


@pytest.mark.asyncio
async def test_settle_pending_state_update_waits_for_background_task(monkeypatch):
    started = False

    async def fake_update_state(_self, *_args):
        nonlocal started
        started = True

    monkeypatch.setattr(server_module.narrator.__class__, "update_state", fake_update_state)
    server_module._pending_state_update_task = None

    server_module._start_state_update()
    assert server_module._pending_state_update_task is not None

    await server_module._settle_pending_state_update()

    assert started is True
    assert server_module._pending_state_update_task is None


@pytest.mark.asyncio
async def test_settle_pending_state_update_cancels_background_task(monkeypatch):
    release = False

    async def fake_update_state(_self, *_args):
        while not release:
            await server_module.asyncio.sleep(0)

    monkeypatch.setattr(server_module.narrator.__class__, "update_state", fake_update_state)
    server_module._pending_state_update_task = None

    server_module._start_state_update()
    task = server_module._pending_state_update_task
    assert task is not None

    await server_module._settle_pending_state_update(cancel=True)

    assert task.cancelled()
    assert server_module._pending_state_update_task is None


@pytest.mark.asyncio
async def test_api_save_returns_error_detail(monkeypatch):
    async def fake_export_save_archive_with_detail(*, target_filename=None):
        assert target_filename is None
        return None, "sqlite 已锁定"

    logged: list[str] = []

    def fake_log_error(message, *args):
        logged.append(message % args if args else message)

    monkeypatch.setattr(
        server_module,
        "export_save_archive_with_detail",
        fake_export_save_archive_with_detail,
    )
    monkeypatch.setattr(server_module.routing_logger, "error", fake_log_error)
    server_module._pending_state_update_task = None

    response = await server_module.api_save()

    assert response.status_code == 500
    assert json.loads(response.body) == {"ok": False, "detail": "sqlite 已锁定"}
    assert logged == ["[save] /api/save 失败: sqlite 已锁定"]


@pytest.mark.asyncio
async def test_api_save_passes_target_filename(monkeypatch):
    called: list[str | None] = []

    async def fake_export_save_archive_with_detail(*, target_filename=None):
        called.append(target_filename)
        return "/tmp/school_slot.zip", None

    monkeypatch.setattr(
        server_module,
        "export_save_archive_with_detail",
        fake_export_save_archive_with_detail,
    )
    server_module._pending_state_update_task = None

    response = await server_module.api_save(
        server_module.SaveRequest(filename="school_slot.zip")
    )

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "ok": True,
        "path": "/tmp/school_slot.zip",
        "filename": "school_slot.zip",
    }
    assert called == ["school_slot.zip"]


@pytest.mark.asyncio
async def test_api_save_catches_unhandled_exception(monkeypatch):
    async def fake_settle_pending_state_update(*, cancel=False):
        raise RuntimeError("pending task 爆了")

    logged: list[str] = []

    def fake_log_error(message, *args):
        logged.append(message % args if args else message)

    monkeypatch.setattr(
        server_module,
        "_settle_pending_state_update",
        fake_settle_pending_state_update,
    )
    monkeypatch.setattr(server_module.routing_logger, "error", fake_log_error)

    response = await server_module.api_save()

    assert response.status_code == 500
    assert json.loads(response.body) == {
        "ok": False,
        "detail": "RuntimeError: pending task 爆了",
    }
    assert len(logged) == 1
    assert logged[0].startswith("[save] /api/save 未捕获异常: RuntimeError: pending task 爆了")


@pytest.mark.asyncio
async def test_api_memory_graph_links_understandings_to_episodes(monkeypatch):
    monkeypatch.setattr(
        server_module,
        "get_agent_names",
        lambda include_narrator=True: ["alice"] if not include_narrator else ["alice", "narrator"],
    )
    monkeypatch.setattr(
        server_module,
        "_get_agent_display_name",
        lambda agent_name: "Alice" if agent_name == "alice" else agent_name,
    )
    monkeypatch.setattr(
        server_module,
        "read_memory_jsonl",
        lambda agent_name: [
            server_module.EpisodeMemory(
                id="e1",
                date="4月3日",
                title="旧阅览室",
                content="她请求玩家保密。",
                importance=4,
                keywords=["保密"],
            )
        ],
    )
    monkeypatch.setattr(
        server_module,
        "read_understandings",
        lambda agent_name: {
            "u1": server_module.Understanding(
                id="u1",
                subject="玩家值得信任",
                content="玩家会认真履行约定。",
                keywords=["信任"],
                linked_episodes=["e1", "missing-e2"],
            )
        },
    )

    response = await server_module.api_memory_graph("alice")
    body = json.loads(response.body)

    assert response.status_code == 200
    assert body["agents"] == [
        {
            "name": "alice",
            "display_name": "Alice",
            "episode_count": 1,
            "understanding_count": 1,
            "edge_count": 2,
        }
    ]
    assert body["selected_agent"] == "alice"
    assert body["stats"] == {
        "episode_count": 1,
        "understanding_count": 1,
        "edge_count": 2,
        "missing_episode_count": 1,
    }
    assert {node["id"] for node in body["nodes"]} == {
        "episode:e1",
        "understanding:u1",
        "episode:missing-e2",
    }
    assert {edge["to"] for edge in body["edges"]} == {"episode:e1", "episode:missing-e2"}


@pytest.mark.asyncio
async def test_api_memory_graph_rejects_unknown_agent(monkeypatch):
    monkeypatch.setattr(
        server_module,
        "get_agent_names",
        lambda include_narrator=True: ["alice"] if not include_narrator else ["alice", "narrator"],
    )
    monkeypatch.setattr(server_module, "read_memory_jsonl", lambda agent_name: [])
    monkeypatch.setattr(server_module, "read_understandings", lambda agent_name: {})

    response = await server_module.api_memory_graph("bob")

    assert response.status_code == 404
    assert json.loads(response.body) == {"detail": "角色不存在。"}


@pytest.mark.asyncio
async def test_chat_stream_emits_created_character_identity(monkeypatch):
    async def fake_settle_pending_state_update(*, cancel=False):
        return None

    async def fake_route(_self, _user_input):
        return [], "", [], True

    async def fake_bootstrap_new_characters(_specs, _targets):
        return [], [
            SimpleNamespace(
                character_id="mitsukimom",
                display_name="桥本志津",
                identity="美月的妈妈，来学校接她放学的家长。",
            )
        ]

    async def fake_broadcast_player_message(_targets, _user_input):
        return None

    monkeypatch.setattr(
        server_module,
        "_settle_pending_state_update",
        fake_settle_pending_state_update,
    )
    monkeypatch.setattr(server_module.narrator.__class__, "route", fake_route)
    monkeypatch.setattr(
        server_module,
        "bootstrap_new_characters",
        fake_bootstrap_new_characters,
    )
    monkeypatch.setattr(
        server_module.message_router,
        "broadcast_player_message",
        fake_broadcast_player_message,
    )

    chunks = [chunk async for chunk in server_module._chat_stream("来个新角色")]

    assert len(chunks) == 2
    created_event = json.loads(chunks[0].removeprefix("data: ").strip())
    assert created_event == {
        "type": "system",
        "title": "角色已创建",
        "name": "桥本志津",
        "identity": "美月的妈妈，来学校接她放学的家长。",
        "character_id": "mitsukimom",
    }
    done_event = json.loads(chunks[1].removeprefix("data: ").strip())
    assert done_event == {"type": "done"}
