"""测试 relations.md 存储 + prompt_builder 关系块 + Character 写回。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


project_root = Path(__file__).parent.parent
os.chdir(project_root)

try:
    import engine.prompt_builder as prompt_builder
    from engine import world_schedule as world_schedule_module
    from engine.agent_schema import CharacterOutput
    from engine.character import Character
    from storage import agent_files
    from storage.agent_files import (
        read_relations,
        read_relations_section,
        update_relations,
    )
except ModuleNotFoundError as exc:
    pytest.skip(f"skip relations tests: missing dependency ({exc})", allow_module_level=True)


def _write_character(
    root: Path,
    name: str,
    *,
    soul: str = "",
    status: str = "",
    relations: str = "",
) -> None:
    agent_dir = root / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    if soul:
        (agent_dir / "soul.md").write_text(soul, encoding="utf-8")
    if status:
        (agent_dir / "status.md").write_text(status, encoding="utf-8")
    if relations:
        (agent_dir / "relations.md").write_text(relations, encoding="utf-8")


@pytest.fixture
def character_dir(tmp_path: Path, monkeypatch):
    from shared import config as shared_config

    monkeypatch.setattr(shared_config, "CHARACTERS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def patched_agents(monkeypatch):
    def _patch(names: list[str]):
        for module in (prompt_builder, world_schedule_module, agent_files):
            if hasattr(module, "get_agent_names"):
                monkeypatch.setattr(
                    module, "get_agent_names", lambda include_narrator=False: names
                )

    return _patch


# ---------------------------------------------------------------------------
# storage.agent_files: update_relations / read_relations
# ---------------------------------------------------------------------------


def test_update_relations_creates_file_then_overwrites(character_dir):
    _write_character(character_dir, "mitsuki")

    result1 = update_relations("mitsuki", "player", "刚认识，还在试探")
    assert result1["operation"] == "append"
    assert read_relations_section("mitsuki", "player") == "刚认识，还在试探"

    result2 = update_relations("mitsuki", "player", "今天开始觉得他靠谱")
    assert result2["operation"] == "replace"
    assert result2["before"] == "刚认识，还在试探"
    assert read_relations_section("mitsuki", "player") == "今天开始觉得他靠谱"


def test_update_relations_preserves_other_sections(character_dir):
    seed = "# 我眼中的其他人\n\n## player\n初始视角\n\n## lilith\n同班同学\n"
    _write_character(character_dir, "mitsuki", relations=seed)

    update_relations("mitsuki", "player", "新视角")
    sections = read_relations("mitsuki")
    assert sections["player"] == "新视角"
    assert sections["lilith"] == "同班同学"


def test_update_relations_skips_empty_content(character_dir):
    _write_character(character_dir, "mitsuki")
    result = update_relations("mitsuki", "player", "   ")
    assert result["operation"] == "skip"
    assert read_relations_section("mitsuki", "player") == ""


def test_update_relations_skips_empty_target(character_dir):
    _write_character(character_dir, "mitsuki")
    result = update_relations("mitsuki", "  ", "随便")
    assert result["operation"] == "skip"


# ---------------------------------------------------------------------------
# build_relations_block: character audience
# ---------------------------------------------------------------------------


def test_build_relations_block_character_renders_first_person_only(
    character_dir, patched_agents
):
    _write_character(
        character_dir,
        "mitsuki",
        soul="# 美月\n",
        relations=(
            "# 我眼中的其他人\n\n"
            "## lilith\n同班，关系平淡\n"
        ),
    )
    _write_character(
        character_dir,
        "lilith",
        soul="# 莉莉丝\n",
        relations="# 我眼中的其他人\n\n## mitsuki\n座位前后的同学\n",
    )
    patched_agents(["mitsuki", "lilith"])

    block = prompt_builder.build_relations_block("mitsuki", scene_targets=["lilith"])

    assert block.startswith("<relations>")
    assert "- lilith / 莉莉丝：同班，关系平淡" in block
    # 对玩家的视角由 user.md / status 承载，不应出现在 relations 块中
    assert "玩家" not in block
    # 不应泄露其他角色对自己的看法 —— 角色应保持第一人称视角
    assert "座位前后的同学" not in block


def test_build_relations_block_character_empty_when_no_candidates(
    character_dir, patched_agents
):
    _write_character(character_dir, "mitsuki", soul="# 美月\n")
    patched_agents(["mitsuki"])

    block = prompt_builder.build_relations_block("mitsuki", scene_targets=[])
    # 候选集为空（对玩家的视角不走 relations）→ 不渲染块
    assert block == ""


# ---------------------------------------------------------------------------
# build_relations_block: narrator audience
# ---------------------------------------------------------------------------


def test_build_relations_block_narrator_lists_each_source(character_dir, patched_agents):
    _write_character(
        character_dir,
        "narrator",
        status="## 当前时间\n4月3日 星期一 8:23\n\n## 场景\n教室\n",
    )
    _write_character(
        character_dir,
        "mitsuki",
        soul="# 美月\n",
        status="## 当前位置\n教室\n",
        relations="# 我眼中的其他人\n\n## lilith\nM 看 L\n",
    )
    _write_character(
        character_dir,
        "lilith",
        soul="# 莉莉丝\n",
        status="## 当前位置\n教室\n",
        relations="# 我眼中的其他人\n\n## mitsuki\nL 看 M\n",
    )
    patched_agents(["mitsuki", "lilith"])

    block = prompt_builder.build_relations_block("narrator")

    assert "## mitsuki / 美月 对其他人的看法" in block
    assert "## lilith / 莉莉丝 对其他人的看法" in block
    assert "- lilith / 莉莉丝：M 看 L" in block
    assert "- mitsuki / 美月：L 看 M" in block
    # narrator 视角也不再关心每人对玩家的看法（那是 user.md / status 的事）
    assert "玩家" not in block


def test_build_relations_block_narrator_skips_offstage_agents(character_dir, patched_agents):
    _write_character(
        character_dir,
        "narrator",
        status="## 当前时间\n4月3日 星期一 8:23\n\n## 场景\n教室\n",
    )
    _write_character(
        character_dir,
        "mitsuki",
        soul="# 美月\n",
        status="## 当前位置\n教室\n",
        relations="# 我眼中的其他人\n\n## player\nM 看 P\n",
    )
    _write_character(
        character_dir,
        "lilith",
        soul="# 莉莉丝\n",
        status="## 当前位置\n图书馆\n",
        relations="# 我眼中的其他人\n\n## player\n不该出现\n",
    )
    patched_agents(["mitsuki", "lilith"])

    block = prompt_builder.build_relations_block("narrator")
    assert "mitsuki" in block
    assert "lilith" not in block
    assert "不该出现" not in block


# ---------------------------------------------------------------------------
# build_user_message 注入位置
# ---------------------------------------------------------------------------


def test_build_user_message_injects_relations_for_character(character_dir, patched_agents):
    _write_character(
        character_dir,
        "mitsuki",
        soul="# 美月\n",
        status="## 当前位置\n教室\n",
        relations="# 我眼中的其他人\n\n## lilith\n同班同学\n",
    )
    _write_character(character_dir, "lilith", soul="# 莉莉丝\n")
    patched_agents(["mitsuki", "lilith"])

    message, _ = prompt_builder.build_user_message(
        "mitsuki", "你好", "", raw_messages=[], scene_targets=["lilith"]
    )

    assert "<relations>" in message
    assert "同班同学" in message


def test_build_user_message_injects_relations_for_narrator(character_dir, patched_agents):
    _write_character(
        character_dir,
        "narrator",
        status="## 当前时间\n4月3日 星期一 8:23\n\n## 场景\n教室\n",
    )
    _write_character(
        character_dir,
        "mitsuki",
        soul="# 美月\n",
        status="## 当前位置\n教室\n",
        relations="# 我眼中的其他人\n\n## player\nM 看 P\n",
    )
    patched_agents(["mitsuki"])

    message, _ = prompt_builder.build_user_message(
        "narrator", "玩家说话", "", raw_messages=[]
    )

    assert "<relations>" in message
    assert "## mitsuki / 美月 对其他人的看法" in message


# ---------------------------------------------------------------------------
# Character._apply_updates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_character_apply_updates_writes_valid_relations(character_dir, patched_agents):
    _write_character(
        character_dir,
        "mitsuki",
        soul="# 美月\n",
        status="## 心境\n平静\n",
    )
    _write_character(character_dir, "lilith", soul="# 莉莉丝\n")
    patched_agents(["mitsuki", "lilith"])

    output = CharacterOutput(
        content="...",
        memory="",
        relations={
            "player": "对玩家的视角应走 user.md / status，不写进 relations",
            "lilith": "同班好友",
            "ghost": "不应该被写入",
            "mitsuki": "不能对自己写关系",
        },
    )
    char = Character("mitsuki")
    await char._apply_updates(output)

    sections = read_relations("mitsuki")
    assert sections["lilith"] == "同班好友"
    assert "player" not in sections
    assert "ghost" not in sections
    assert "mitsuki" not in sections
