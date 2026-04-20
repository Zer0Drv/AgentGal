"""测试 world.schedule：解析、查询、时段切换。"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agents.schema import CharacterSchedule
from world.schedule import (
    collect_default_locations,
    detect_slot_change,
    find_slot,
    get_default_location,
    load_character_schedule,
    parse_game_time,
    query_all_locations,
    query_who_is_here,
    save_character_schedule,
)


MITSUKI_SCHEDULE = {
    "periods": [
        {
            "start": "2026-04-01",
            "end": "2026-07-31",
            "name": "春学期",
            "slots": [
                {"days": ["mon", "tue", "wed", "thu", "fri"], "time": "上午", "location": "教室"},
                {"days": ["mon", "tue", "wed", "thu", "fri"], "time": "下午", "location": "社团室"},
                {"days": ["sat", "sun"], "time": "全天", "location": "家"},
            ],
        }
    ]
}

LILITH_SCHEDULE = {
    "periods": [
        {
            "start": "2026-04-01",
            "end": "2026-07-31",
            "name": "春学期",
            "slots": [
                {"days": ["mon", "tue", "wed", "thu", "fri"], "time": "上午", "location": "教室"},
                {"days": ["mon", "tue", "wed", "thu", "fri"], "time": "下午", "location": "图书馆"},
                {"days": ["sat", "sun"], "time": "全天", "location": "咖啡店"},
            ],
        }
    ]
}


@pytest.fixture
def mitsuki() -> CharacterSchedule:
    return CharacterSchedule.model_validate(MITSUKI_SCHEDULE)


@pytest.fixture
def character_dir(tmp_path: Path, monkeypatch):
    """把 CHARACTERS_DIR 指向 tmp_path，让 load/save 读写隔离目录。"""
    from shared import config as shared_config

    monkeypatch.setattr(shared_config, "CHARACTERS_DIR", tmp_path)
    return tmp_path


def _write_schedule(root: Path, agent: str, data: dict) -> None:
    agent_dir = root / agent
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "schedule.json").write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# 时间解析
# ---------------------------------------------------------------------------


def test_parse_game_time_weekday_and_hour():
    assert parse_game_time("4月3日 星期一 8:23") == ("mon", "上午")
    assert parse_game_time("10月2日 星期五 09:20") == ("fri", "上午")
    assert parse_game_time("星期三 14:00") == ("wed", "下午")
    assert parse_game_time("星期日 20:00") == ("sun", "晚上")


def test_parse_game_time_with_explicit_bucket():
    assert parse_game_time("星期六 晚上") == ("sat", "晚上")
    assert parse_game_time("星期天 全天") == ("sun", "全天")


def test_parse_game_time_invalid():
    assert parse_game_time("") is None
    assert parse_game_time("不知道是什么时候") is None
    assert parse_game_time("星期一") is None


# ---------------------------------------------------------------------------
# 单角色查询
# ---------------------------------------------------------------------------


def test_find_slot_matches_weekday_and_bucket(mitsuki):
    slot = find_slot(mitsuki, ("mon", "上午"), date="2026-04-15")
    assert slot is not None
    assert slot.location == "教室"

    slot = find_slot(mitsuki, ("sat", "上午"), date="2026-04-18")
    assert slot is not None
    assert slot.time == "全天"
    assert slot.location == "家"


def test_find_slot_outside_period_returns_none(mitsuki):
    assert find_slot(mitsuki, ("mon", "上午"), date="2026-01-01") is None


def test_find_slot_accepts_cn_game_date(mitsuki):
    slot = find_slot(mitsuki, ("mon", "上午"), date="4月15日 星期一 08:20")
    assert slot is not None
    assert slot.location == "教室"


def test_find_slot_prefers_explicit_bucket_over_fallback():
    schedule = CharacterSchedule.model_validate(
        {
            "periods": [
                {
                    "start": "2026-04-01",
                    "end": "2026-07-31",
                    "slots": [
                        {"days": ["mon"], "time": "全天", "location": "家"},
                        {"days": ["mon"], "time": "上午", "location": "咖啡店"},
                    ],
                }
            ]
        }
    )
    slot = find_slot(schedule, ("mon", "上午"), date="2026-04-15")
    assert slot.location == "咖啡店"


# ---------------------------------------------------------------------------
# 加载 / 保存 / 聚合（基于 tmp character dir）
# ---------------------------------------------------------------------------


def test_load_missing_returns_empty(character_dir):
    assert load_character_schedule("nobody").periods == []


def test_save_and_reload_roundtrip(character_dir, mitsuki):
    save_character_schedule("mitsuki", mitsuki)
    loaded = load_character_schedule("mitsuki")
    assert loaded == mitsuki


def test_load_rejects_invalid_weekday(character_dir):
    _write_schedule(
        character_dir,
        "broken",
        {
            "periods": [
                {
                    "start": "2026-04-01",
                    "end": "2026-07-31",
                    "slots": [{"days": ["xxx"], "time": "上午", "location": "教室"}],
                }
            ]
        },
    )
    with pytest.raises(Exception):
        load_character_schedule("broken")


def test_get_default_location(character_dir):
    _write_schedule(character_dir, "mitsuki", MITSUKI_SCHEDULE)
    assert get_default_location("mitsuki", ("mon", "下午"), "2026-04-15") == "社团室"
    assert get_default_location("nobody", ("mon", "下午"), "2026-04-15") is None


def _patch_agents(names: list[str]):
    return patch("world.schedule.get_agent_names", return_value=names)


def test_collect_default_locations(character_dir):
    _write_schedule(character_dir, "mitsuki", MITSUKI_SCHEDULE)
    _write_schedule(character_dir, "lilith", LILITH_SCHEDULE)
    with _patch_agents(["mitsuki", "lilith"]):
        result = collect_default_locations(("mon", "上午"), "2026-04-15")
    assert result == {"mitsuki": "教室", "lilith": "教室"}


def test_query_all_locations_real_overrides_schedule(character_dir):
    _write_schedule(character_dir, "mitsuki", MITSUKI_SCHEDULE)
    _write_schedule(character_dir, "lilith", LILITH_SCHEDULE)
    with _patch_agents(["mitsuki", "lilith"]):
        result = query_all_locations(
            ("mon", "上午"),
            real_locations={"mitsuki": "天台"},
            date="2026-04-15",
        )
    assert result["天台"] == ["mitsuki"]
    assert result["教室"] == ["lilith"]


def test_query_who_is_here(character_dir):
    _write_schedule(character_dir, "mitsuki", MITSUKI_SCHEDULE)
    _write_schedule(character_dir, "lilith", LILITH_SCHEDULE)
    with _patch_agents(["mitsuki", "lilith"]):
        here = query_who_is_here(
            "教室", ("mon", "上午"), real_locations={}, date="2026-04-15"
        )
    assert sorted(here) == ["lilith", "mitsuki"]


# ---------------------------------------------------------------------------
# 时段切换
# ---------------------------------------------------------------------------


def test_detect_slot_change_bucket_boundary():
    assert detect_slot_change("4月3日 星期一 8:23", "4月3日 星期一 13:00")
    assert not detect_slot_change("4月3日 星期一 8:23", "4月3日 星期一 11:00")


def test_detect_slot_change_weekday_boundary():
    assert detect_slot_change("星期一 晚上", "星期二 晚上")
