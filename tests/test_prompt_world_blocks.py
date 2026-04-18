"""测试 prompt_builder 的 <world_now> 与 <my_schedule> 块渲染。"""

import json
import os
from pathlib import Path

import pytest


project_root = Path(__file__).parent.parent
os.chdir(project_root)

try:
    import engine.prompt_builder as prompt_builder
    from engine import world_schedule as world_schedule_module
except ModuleNotFoundError as exc:
    pytest.skip(f"skip prompt world block tests: missing dependency ({exc})", allow_module_level=True)


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
            ],
        }
    ]
}


def _write_character(
    root: Path,
    name: str,
    status_body: str,
    *,
    soul: str = "",
    user: str = "",
    schedule: dict | None = None,
) -> None:
    agent_dir = root / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "status.md").write_text(
        f"# {name} 的状态\n\n{status_body}\n",
        encoding="utf-8",
    )
    if soul:
        (agent_dir / "soul.md").write_text(soul, encoding="utf-8")
    if user:
        (agent_dir / "user.md").write_text(user, encoding="utf-8")
    if schedule is not None:
        (agent_dir / "schedule.json").write_text(json.dumps(schedule), encoding="utf-8")


@pytest.fixture
def character_dir(tmp_path: Path, monkeypatch):
    from shared import config as shared_config

    monkeypatch.setattr(shared_config, "CHARACTERS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def patched_agents(monkeypatch):
    def _patch(names: list[str]):
        monkeypatch.setattr(
            prompt_builder, "get_agent_names", lambda include_narrator=False: names
        )
        monkeypatch.setattr(
            world_schedule_module,
            "get_agent_names",
            lambda include_narrator=False: names,
        )

    return _patch


# ---------------------------------------------------------------------------
# <world_now>
# ---------------------------------------------------------------------------


def test_world_now_empty_when_no_narrator_status(character_dir, patched_agents):
    patched_agents([])
    assert prompt_builder.build_world_now_block() == ""


def test_world_now_merges_real_and_schedule_locations(character_dir, patched_agents):
    narrator_status = (
        "## 当前时间\n4月3日 星期一 8:23\n\n## 场景\n图书馆旧阅览角\n"
    )
    _write_character(character_dir, "narrator", narrator_status)
    _write_character(
        character_dir,
        "mitsuki",
        "## 当前位置\n图书馆旧阅览角\n",
        soul="# 美月\n",
        schedule=MITSUKI_SCHEDULE,
    )
    _write_character(
        character_dir,
        "lilith",
        "## 当前位置\n\n",
        soul="# 莉莉丝\n",
        schedule=LILITH_SCHEDULE,
    )
    patched_agents(["mitsuki", "lilith"])

    block = prompt_builder.build_world_now_block()

    assert block.startswith("<world_now>")
    assert block.endswith("</world_now>")
    assert "当前时间：4月3日 星期一 8:23" in block
    assert "当前场景：图书馆旧阅览角" in block
    assert "图书馆旧阅览角（当前场景）" in block
    assert "mitsuki / 美月" in block
    assert "- 教室：lilith / 莉莉丝" in block


def test_world_now_without_parseable_time_still_lists_real_locations(
    character_dir, patched_agents
):
    narrator_status = "## 当前时间\n\n## 场景\n天台\n"
    _write_character(character_dir, "narrator", narrator_status)
    _write_character(
        character_dir, "mitsuki", "## 当前位置\n天台\n", soul="# 美月\n"
    )
    _write_character(
        character_dir, "lilith", "## 当前位置\n教室\n", soul="# 莉莉丝\n"
    )
    patched_agents(["mitsuki", "lilith"])

    block = prompt_builder.build_world_now_block()

    assert "天台（当前场景）：mitsuki / 美月" in block
    assert "- 教室：lilith / 莉莉丝" in block


# ---------------------------------------------------------------------------
# <my_schedule>
# ---------------------------------------------------------------------------


def test_my_schedule_renders_weekday_groupings(character_dir, patched_agents):
    _write_character(
        character_dir, "mitsuki", "## 当前位置\n教室\n", schedule=MITSUKI_SCHEDULE
    )
    patched_agents(["mitsuki"])

    block = prompt_builder.build_my_schedule_block("mitsuki")

    assert block.startswith("<my_schedule>")
    assert "春学期（2026-04-01 至 2026-07-31）" in block
    assert "- 周一至周五 上午：教室" in block
    assert "- 周一至周五 下午：社团室" in block
    assert "- 周末 全天：家" in block


def test_my_schedule_empty_for_narrator(character_dir, patched_agents):
    patched_agents([])
    assert prompt_builder.build_my_schedule_block("narrator") == ""


def test_my_schedule_empty_when_no_schedule_file(character_dir, patched_agents):
    _write_character(character_dir, "mitsuki", "## 当前位置\n教室\n")
    patched_agents(["mitsuki"])
    assert prompt_builder.build_my_schedule_block("mitsuki") == ""


# ---------------------------------------------------------------------------
# build_user_message 注入位置
# ---------------------------------------------------------------------------


def test_build_user_message_injects_world_now_for_narrator(character_dir, patched_agents):
    narrator_status = "## 当前时间\n4月3日 星期一 8:23\n\n## 场景\n教室\n"
    _write_character(character_dir, "narrator", narrator_status)
    _write_character(
        character_dir, "mitsuki", "## 当前位置\n教室\n", soul="# 美月\n"
    )
    patched_agents(["mitsuki"])

    message, _ = prompt_builder.build_user_message(
        "narrator", "玩家新消息内容", "", raw_messages=[]
    )

    assert "<world_now>" in message
    assert "<my_schedule>" not in message


def test_build_user_message_injects_player_views_for_narrator(character_dir, patched_agents):
    narrator_status = "## 当前时间\n4月3日 星期一 8:23\n\n## 场景\n教室\n"
    _write_character(character_dir, "narrator", narrator_status)
    _write_character(
        character_dir,
        "mitsuki",
        "## 当前位置\n教室\n\n## 和玩家的关系\n刚开始在意\n",
        soul="# 美月\n",
        user=(
            "# 美月眼中的玩家\n\n"
            "## 对方是什么人\n- 很会照顾气氛\n\n"
            "## 我们怎么相处\n- 她会先用玩笑试探玩家的反应\n"
        ),
    )
    patched_agents(["mitsuki"])

    message, _ = prompt_builder.build_user_message(
        "narrator", "玩家新消息内容", "", raw_messages=[]
    )

    assert "<player_views>" in message
    assert "## mitsuki / 美月 对玩家" in message
    assert "- 和玩家的关系：刚开始在意" in message
    assert "很会照顾气氛" in message
    assert "先用玩笑试探玩家的反应" in message


def test_build_user_message_injects_my_schedule_for_character(character_dir, patched_agents):
    _write_character(character_dir, "narrator", "## 当前时间\n\n## 场景\n\n")
    _write_character(
        character_dir, "mitsuki", "## 当前位置\n教室\n", schedule=MITSUKI_SCHEDULE
    )
    patched_agents(["mitsuki"])

    message, _ = prompt_builder.build_user_message(
        "mitsuki", "你好", "", raw_messages=[]
    )

    assert "<my_schedule>" in message
    assert "<world_now>" not in message
