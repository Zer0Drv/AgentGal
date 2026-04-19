"""测试 engine.world_sync.post_turn_world_sync。

当前职责：为出场 targets 写入 `.last_seen.json`；位置不再由 world_sync 维护。
"""

import json
from pathlib import Path

import pytest

from engine import world_sync as world_sync_module


@pytest.fixture
def character_dir(tmp_path: Path, monkeypatch):
    from shared import config as shared_config

    monkeypatch.setattr(shared_config, "CHARACTERS_DIR", tmp_path)
    return tmp_path


def _read_last_seen(root: Path, agent: str) -> dict | None:
    path = root / agent / ".last_seen.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def test_writes_last_seen_for_each_target(character_dir):
    (character_dir / "mitsuki").mkdir()
    (character_dir / "yuki").mkdir()

    world_sync_module.post_turn_world_sync(
        targets=["mitsuki", "yuki"],
        now_time="4月3日 星期一 13:00",
    )

    assert _read_last_seen(character_dir, "mitsuki") == {"last_seen": "4月3日 星期一 13:00"}
    assert _read_last_seen(character_dir, "yuki") == {"last_seen": "4月3日 星期一 13:00"}


def test_skips_when_now_time_empty(character_dir):
    (character_dir / "mitsuki").mkdir()

    world_sync_module.post_turn_world_sync(targets=["mitsuki"], now_time="")

    assert _read_last_seen(character_dir, "mitsuki") is None


def test_does_not_touch_non_targets(character_dir):
    (character_dir / "mitsuki").mkdir()
    (character_dir / "yuki").mkdir()

    world_sync_module.post_turn_world_sync(
        targets=["mitsuki"],
        now_time="4月3日 星期一 13:00",
    )

    assert _read_last_seen(character_dir, "mitsuki") is not None
    assert _read_last_seen(character_dir, "yuki") is None


def test_empty_targets_is_noop(character_dir):
    world_sync_module.post_turn_world_sync(targets=[], now_time="4月3日 星期一 13:00")
    assert not any(character_dir.iterdir())
