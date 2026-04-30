from pathlib import Path

import memory.parser as parser_module
from memory.parser import EpisodeMemory, append_memory_records, read_memory_jsonl


def test_append_memory_records_assigns_episode_ids(tmp_path, monkeypatch):
    def _character_path(agent_name: str, *subpaths: str) -> str:
        return str(tmp_path / agent_name / Path(*subpaths))

    monkeypatch.setattr(parser_module, "character_path", _character_path)

    written = append_memory_records(
        "alice",
        [
            EpisodeMemory(content="需要生成 id 的记忆。"),
            EpisodeMemory(id="stable-id", content="已有 id 的记忆。"),
            EpisodeMemory(content="   "),
        ],
    )

    assert len(written) == 2
    assert written[0].id
    assert written[0].id != "stable-id"
    assert written[0].memory_owner == "alice"
    assert written[1].id == "stable-id"
    assert written[1].memory_owner == "alice"

    persisted = read_memory_jsonl("alice")
    assert [record.id for record in persisted] == [written[0].id, "stable-id"]
