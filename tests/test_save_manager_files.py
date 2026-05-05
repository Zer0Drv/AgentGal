"""测试 storage.save_manager._get_agent_save_files 覆盖到的文件清单。"""

from __future__ import annotations

import json
import hashlib
import zipfile
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
    (agent_dir / "understanding.jsonl").write_text(
        '{"id":"u1","content":"理解。"}\n', encoding="utf-8"
    )
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
    assert "understanding.jsonl" in basenames


def test_missing_schedule_is_omitted(character_dir: Path):
    _seed_character(character_dir, "mitsuki")

    files = save_manager._get_agent_save_files("mitsuki")
    basenames = {Path(f).name for f in files}

    assert "schedule.json" not in basenames
    # 现有文件正常覆盖
    assert "soul.md" in basenames
    assert "understanding.jsonl" in basenames


def test_character_save_files_omit_legacy_recall_sidecar(character_dir: Path):
    agent_dir = _seed_character(character_dir, "mitsuki")
    (agent_dir / ".memory_recall_state.json").write_text("{}", encoding="utf-8")

    files = save_manager._get_agent_save_files("mitsuki")
    basenames = {Path(f).name for f in files}

    assert ".memory_recall_state.json" not in basenames


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


def test_memory_jsonl_archive_payload_merges_db_recall_state(
    character_dir: Path,
    monkeypatch,
):
    from memory.parser import EpisodeMemory, parse_jsonl_line, serialize_episode

    def _character_path(agent_name: str, *parts: str) -> str:
        return str(character_dir / agent_name / Path(*parts))

    monkeypatch.setattr(save_manager, "character_path", _character_path)

    agent_dir = character_dir / "mitsuki"
    agent_dir.mkdir(parents=True, exist_ok=True)
    episode = EpisodeMemory(
        date="4月3日",
        content="需要合并最新召回日期的记忆。",
        memory_owner="mitsuki",
    )
    (agent_dir / "memory.jsonl").write_text(
        serialize_episode(episode) + "\n",
        encoding="utf-8",
    )
    content_hash = hashlib.sha1(episode.content.encode("utf-8")).hexdigest()

    payload = save_manager._memory_jsonl_archive_payload(
        "mitsuki",
        {
            "1": {
                "date": "4月3日",
                "content_hash": content_hash,
                "last_recalled_at": "4月8日",
            }
        },
    )

    assert payload is not None
    archived = parse_jsonl_line(payload)
    assert archived is not None
    assert archived.last_recalled_at == "4月8日"
    original = parse_jsonl_line((agent_dir / "memory.jsonl").read_text(encoding="utf-8"))
    assert original is not None
    assert original.last_recalled_at == "4月3日"


@pytest.mark.asyncio
async def test_export_new_save_uses_fresh_slot_filename(tmp_path: Path, monkeypatch):
    characters_dir = tmp_path / "data" / "runtime" / "characters"
    narrator_dir = characters_dir / "narrator"
    narrator_dir.mkdir(parents=True)
    (characters_dir / ".story_id").write_text("school", encoding="utf-8")
    (characters_dir / ".turn_counter.json").write_text('{"turn": 3}', encoding="utf-8")
    (narrator_dir / "soul.md").write_text("# narrator\n", encoding="utf-8")
    (narrator_dir / "status.md").write_text("## 叙事焦点\n屋顶\n", encoding="utf-8")

    monkeypatch.setattr(save_manager, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(save_manager, "CHARACTERS_DIR", characters_dir)
    monkeypatch.setattr(save_manager, "get_agent_names", lambda: ["narrator"])
    monkeypatch.setattr(
        save_manager,
        "character_path",
        lambda agent_name, *parts: str(characters_dir / agent_name / Path(*parts)),
    )
    monkeypatch.setattr(save_manager, "_read_narrator_focus", lambda: "屋顶")

    save_path, error = await save_manager.export_save_archive_with_detail()

    assert error is None
    assert save_path is not None
    archive_path = Path(save_path)
    assert archive_path.name.startswith("school_")
    assert archive_path.name.endswith(".zip")

    with zipfile.ZipFile(archive_path) as zf:
        metadata = json.loads(zf.read("metadata.json").decode("utf-8"))
        assert metadata["filename"] == archive_path.name
        assert metadata["save_id"] == archive_path.stem.rsplit("_", 1)[-1]
        assert zf.read(".save_id").decode("utf-8") == metadata["save_id"]

    assert (characters_dir / ".save_id").read_text(encoding="utf-8") == metadata[
        "save_id"
    ]
