"""测试 engine.world_sync.post_turn_world_sync。"""

import json
from pathlib import Path

import pytest

from engine import world_sync as world_sync_module


MITSUKI_SCHEDULE = {
    "periods": [
        {
            "start": "2026-04-01",
            "end": "2026-07-31",
            "name": "春学期",
            "slots": [
                {"days": ["mon"], "time": "上午", "location": "教室"},
                {"days": ["mon"], "time": "下午", "location": "社团室"},
            ],
        }
    ]
}


def _write_character(root: Path, name: str, status_body: str, schedule: dict | None = None) -> None:
    agent_dir = root / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "status.md").write_text(
        f"# {name} 的状态\n\n{status_body}\n",
        encoding="utf-8",
    )
    if schedule is not None:
        (agent_dir / "schedule.json").write_text(json.dumps(schedule), encoding="utf-8")


def _read_status_field(path: Path, field: str) -> str:
    text = path.read_text(encoding="utf-8")
    marker = f"## {field}\n"
    if marker not in text:
        return ""
    rest = text.split(marker, 1)[1]
    return rest.split("\n##", 1)[0].strip()


@pytest.fixture
def character_dir(tmp_path: Path, monkeypatch):
    from shared import config as shared_config

    monkeypatch.setattr(shared_config, "CHARACTERS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def patched_agents(monkeypatch):
    """让 world_sync 能看到测试中 tmp 目录的角色名单。"""

    def _patch(names: list[str]):
        monkeypatch.setattr(
            world_sync_module, "get_agent_names", lambda include_narrator=False: names
        )

    return _patch


def test_targets_location_updated_to_scene(character_dir, patched_agents):
    _write_character(character_dir, "mitsuki", "## 当前位置\n教室\n\n## 打算\n")
    patched_agents(["mitsuki"])

    world_sync_module.post_turn_world_sync(
        scene="图书馆旧阅览角",
        targets=["mitsuki"],
        prev_time="",
        now_time="",
    )

    loc = _read_status_field(character_dir / "mitsuki" / "status.md", "当前位置")
    assert loc == "图书馆旧阅览角"


def test_targets_last_seen_written(character_dir, patched_agents):
    _write_character(character_dir, "mitsuki", "## 当前位置\n教室\n\n## 打算\n")
    patched_agents(["mitsuki"])

    world_sync_module.post_turn_world_sync(
        scene="天台",
        targets=["mitsuki"],
        prev_time="4月3日 星期一 8:23",
        now_time="4月3日 星期一 13:00",
    )

    sidecar = character_dir / "mitsuki" / ".last_seen.json"
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {
        "last_seen": "4月3日 星期一 13:00"
    }


def test_slot_change_applies_schedule_default_to_idle_nontarget(
    character_dir, patched_agents
):
    _write_character(
        character_dir, "mitsuki", "## 当前位置\n教室\n\n## 打算\n", schedule=MITSUKI_SCHEDULE
    )
    patched_agents(["mitsuki"])

    world_sync_module.post_turn_world_sync(
        scene="天台",
        targets=[],
        prev_time="4月3日 星期一 8:23",
        now_time="4月3日 星期一 14:00",
    )

    loc = _read_status_field(character_dir / "mitsuki" / "status.md", "当前位置")
    assert loc == "社团室"


def test_slot_change_skips_nontarget_with_intention(character_dir, patched_agents):
    status_body = "## 当前位置\n教室\n\n## 打算\n- [ ] 【补讲义】找玩家搭话。\n"
    _write_character(character_dir, "mitsuki", status_body, schedule=MITSUKI_SCHEDULE)
    patched_agents(["mitsuki"])

    world_sync_module.post_turn_world_sync(
        scene="天台",
        targets=[],
        prev_time="4月3日 星期一 8:23",
        now_time="4月3日 星期一 14:00",
    )

    loc = _read_status_field(character_dir / "mitsuki" / "status.md", "当前位置")
    assert loc == "教室"


def test_no_slot_change_leaves_nontarget_alone(character_dir, patched_agents):
    _write_character(
        character_dir, "mitsuki", "## 当前位置\n教室\n\n## 打算\n", schedule=MITSUKI_SCHEDULE
    )
    patched_agents(["mitsuki"])

    world_sync_module.post_turn_world_sync(
        scene="天台",
        targets=[],
        prev_time="4月3日 星期一 8:23",
        now_time="4月3日 星期一 9:00",
    )

    loc = _read_status_field(character_dir / "mitsuki" / "status.md", "当前位置")
    assert loc == "教室"


def test_target_overrides_schedule_default_on_slot_change(character_dir, patched_agents):
    """targets 在第一步就被写成 scene，即便跨时段也不被 schedule 默认覆盖。"""
    _write_character(
        character_dir, "mitsuki", "## 当前位置\n教室\n\n## 打算\n", schedule=MITSUKI_SCHEDULE
    )
    patched_agents(["mitsuki"])

    world_sync_module.post_turn_world_sync(
        scene="天台",
        targets=["mitsuki"],
        prev_time="4月3日 星期一 8:23",
        now_time="4月3日 星期一 14:00",
    )

    loc = _read_status_field(character_dir / "mitsuki" / "status.md", "当前位置")
    assert loc == "天台"


def test_empty_scene_does_not_clobber_location(character_dir, patched_agents):
    _write_character(character_dir, "mitsuki", "## 当前位置\n教室\n\n## 打算\n")
    patched_agents(["mitsuki"])

    world_sync_module.post_turn_world_sync(
        scene="",
        targets=["mitsuki"],
        prev_time="",
        now_time="4月3日 星期一 8:23",
    )

    loc = _read_status_field(character_dir / "mitsuki" / "status.md", "当前位置")
    assert loc == "教室"
