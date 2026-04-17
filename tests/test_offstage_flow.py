"""测试 engine.offstage_flow.maybe_synthesize_offstage。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from engine import offstage_flow as offstage_flow_module
from engine.agent_schema import OffstageMemoryBlock


def _write_character(
    root: Path,
    name: str,
    *,
    soul: str = "",
    status: str = "",
    last_seen: str | None = None,
    memory: str = "",
    schedule: dict | None = None,
) -> None:
    agent_dir = root / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "soul.md").write_text(soul, encoding="utf-8")
    (agent_dir / "status.md").write_text(status, encoding="utf-8")
    (agent_dir / "memory.md").write_text(memory, encoding="utf-8")
    if last_seen is not None:
        (agent_dir / ".last_seen.json").write_text(
            json.dumps({"last_seen": last_seen}), encoding="utf-8"
        )
    if schedule is not None:
        (agent_dir / "schedule.json").write_text(json.dumps(schedule), encoding="utf-8")


@pytest.fixture
def character_dir(tmp_path: Path, monkeypatch):
    from shared import config as shared_config

    monkeypatch.setattr(shared_config, "CHARACTERS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def patched_llm(monkeypatch):
    """拦截 offstage_flow 对 LLM / agent 的依赖，返回固定块。"""

    def _patch(
        block: OffstageMemoryBlock | Exception = OffstageMemoryBlock(
            date="4月4日",
            content="- **时间/地点/在场**：周末在家，一个人。心里还是在想那件事。",
        ),
    ):
        if isinstance(block, Exception):
            mock = AsyncMock(side_effect=block)
        else:
            mock = AsyncMock(return_value=block)

        monkeypatch.setattr(offstage_flow_module, "run_structured_agent", mock)
        monkeypatch.setattr(
            offstage_flow_module,
            "get_offstage_synthesizer_agent",
            lambda: object(),
        )
        monkeypatch.setattr(
            offstage_flow_module,
            "get_offstage_synthesizer_llm_config",
            lambda: {"model": "stub"},
        )
        return mock

    return _patch


@pytest.mark.asyncio
async def test_first_time_writes_last_seen_without_catchup(character_dir, patched_llm):
    _write_character(character_dir, "mitsuki", soul="# 美月\n", status="")
    mock_run = patched_llm()

    await offstage_flow_module.maybe_synthesize_offstage("mitsuki", "4月5日 星期三 10:00")

    sidecar = json.loads(
        (character_dir / "mitsuki" / ".last_seen.json").read_text(encoding="utf-8")
    )
    assert sidecar["last_seen"] == "4月5日 星期三 10:00"
    assert mock_run.await_count == 0
    assert (character_dir / "mitsuki" / "memory.md").read_text(encoding="utf-8") == ""


@pytest.mark.asyncio
async def test_gap_below_threshold_is_skipped(character_dir, patched_llm):
    _write_character(
        character_dir,
        "mitsuki",
        soul="# 美月\n",
        last_seen="4月5日 星期三 10:00",
    )
    mock_run = patched_llm()

    await offstage_flow_module.maybe_synthesize_offstage("mitsuki", "4月5日 星期三 16:00")

    assert mock_run.await_count == 0
    assert (character_dir / "mitsuki" / "memory.md").read_text(encoding="utf-8") == ""


@pytest.mark.asyncio
async def test_gap_above_threshold_appends_memory(character_dir, patched_llm):
    _write_character(
        character_dir,
        "mitsuki",
        soul="# 美月\n",
        status="## 打算\n- [ ] 【复习】准备周一考试\n",
        last_seen="4月1日 星期六 10:00",
    )
    mock_run = patched_llm()

    await offstage_flow_module.maybe_synthesize_offstage("mitsuki", "4月5日 星期三 10:00")

    assert mock_run.await_count == 1
    memory = (character_dir / "mitsuki" / "memory.md").read_text(encoding="utf-8")
    assert "4月4日" in memory
    assert "- **时间/地点/在场**" in memory


@pytest.mark.asyncio
async def test_llm_failure_is_logged_and_does_not_raise(character_dir, patched_llm):
    _write_character(
        character_dir,
        "mitsuki",
        soul="# 美月\n",
        last_seen="4月1日 星期六 10:00",
    )
    patched_llm(block=RuntimeError("boom"))

    await offstage_flow_module.maybe_synthesize_offstage("mitsuki", "4月5日 星期三 10:00")

    assert (character_dir / "mitsuki" / "memory.md").read_text(encoding="utf-8") == ""


@pytest.mark.asyncio
async def test_empty_llm_output_is_skipped(character_dir, patched_llm):
    _write_character(
        character_dir,
        "mitsuki",
        soul="# 美月\n",
        last_seen="4月1日 星期六 10:00",
    )
    patched_llm(block=OffstageMemoryBlock(date="   ", content="   "))

    await offstage_flow_module.maybe_synthesize_offstage("mitsuki", "4月5日 星期三 10:00")

    assert (character_dir / "mitsuki" / "memory.md").read_text(encoding="utf-8") == ""


@pytest.mark.asyncio
async def test_empty_now_time_is_noop(character_dir, patched_llm):
    _write_character(character_dir, "mitsuki", soul="# 美月\n")
    mock_run = patched_llm()

    await offstage_flow_module.maybe_synthesize_offstage("mitsuki", "")

    assert mock_run.await_count == 0
    assert not (character_dir / "mitsuki" / ".last_seen.json").exists()
