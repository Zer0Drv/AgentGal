"""测试 server 层 state_updater 后台任务协调。"""

import os
from pathlib import Path

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

    async def fake_run_state_updater(*_args):
        nonlocal started
        started = True

    monkeypatch.setattr(server_module, "run_state_updater", fake_run_state_updater)
    server_module._pending_state_update_task = None

    server_module._start_state_update("玩家输入", "场景", ["mitsuki"], [("mitsuki", "回应")])
    assert server_module._pending_state_update_task is not None

    await server_module._settle_pending_state_update()

    assert started is True
    assert server_module._pending_state_update_task is None


@pytest.mark.asyncio
async def test_settle_pending_state_update_cancels_background_task(monkeypatch):
    release = False

    async def fake_run_state_updater(*_args):
        while not release:
            await server_module.asyncio.sleep(0)

    monkeypatch.setattr(server_module, "run_state_updater", fake_run_state_updater)
    server_module._pending_state_update_task = None

    server_module._start_state_update("玩家输入", "场景", ["mitsuki"], [])
    task = server_module._pending_state_update_task
    assert task is not None

    await server_module._settle_pending_state_update(cancel=True)

    assert task.cancelled()
    assert server_module._pending_state_update_task is None
