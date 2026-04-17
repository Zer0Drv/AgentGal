"""测试 server 层 state_updater 后台任务协调。"""

import json
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

    server_module._start_state_update([])
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

    server_module._start_state_update([])
    task = server_module._pending_state_update_task
    assert task is not None

    await server_module._settle_pending_state_update(cancel=True)

    assert task.cancelled()
    assert server_module._pending_state_update_task is None


@pytest.mark.asyncio
async def test_api_save_returns_error_detail(monkeypatch):
    async def fake_export_save_archive_with_detail():
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
