"""测试 storage.save_manager._get_agent_save_files 覆盖到的文件清单。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from storage import save_manager


@pytest.fixture
def character_dir(tmp_path: Path, monkeypatch):
    """把 CHARACTERS_DIR 指到临时目录，避免污染仓库数据。"""
    from shared import config as shared_config

    monkeypatch.setattr(shared_config, "CHARACTERS_DIR", tmp_path)
    monkeypatch.setattr(save_manager, "CHARACTERS_DIR", tmp_path)
    return tmp_path


def _seed_character(
    root: Path,
    name: str,
    *,
    schedule: dict | None = None,
) -> Path:
    agent_dir = root / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "soul.md").write_text("# soul\n", encoding="utf-8")
    (agent_dir / "status.md").write_text("## 当前位置\n教室\n", encoding="utf-8")
    (agent_dir / "memory.md").write_text("# memory\n", encoding="utf-8")
    (agent_dir / "user.md").write_text("# user\n", encoding="utf-8")
    (agent_dir / "growth.md").write_text("# growth\n", encoding="utf-8")
    (agent_dir / "relations.md").write_text("## player\n友好。\n", encoding="utf-8")
    if schedule is not None:
        (agent_dir / "schedule.json").write_text(
            json.dumps(schedule), encoding="utf-8"
        )
    return agent_dir


def test_character_save_files_include_schedule(character_dir: Path):
    _seed_character(
        character_dir,
        "mitsuki",
        schedule={"periods": []},
    )

    files = save_manager._get_agent_save_files("mitsuki")
    basenames = {Path(f).name for f in files}

    assert "schedule.json" in basenames
    # 已有字段仍然覆盖
    assert "soul.md" in basenames
    assert "relations.md" in basenames


def test_missing_schedule_is_omitted(character_dir: Path):
    _seed_character(character_dir, "mitsuki")

    files = save_manager._get_agent_save_files("mitsuki")
    basenames = {Path(f).name for f in files}

    assert "schedule.json" not in basenames
    # 现有文件正常覆盖
    assert "soul.md" in basenames
    assert "relations.md" in basenames


def test_narrator_does_not_include_character_only_sidecars(character_dir: Path):
    narrator_dir = character_dir / "narrator"
    narrator_dir.mkdir(parents=True, exist_ok=True)
    (narrator_dir / "soul.md").write_text("# narrator\n", encoding="utf-8")
    (narrator_dir / "status.md").write_text("## 当前时间\n4月5日\n", encoding="utf-8")
    # 即便误放了这些文件，narrator 也不应把它们当作存档内容
    (narrator_dir / "schedule.json").write_text("{}", encoding="utf-8")
    (narrator_dir / ".memory_recall_state.json").write_text("{}", encoding="utf-8")

    files = save_manager._get_agent_save_files("narrator")
    basenames = {Path(f).name for f in files}

    assert "schedule.json" not in basenames
    assert ".memory_recall_state.json" not in basenames
    assert "soul.md" in basenames
