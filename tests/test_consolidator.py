"""测试尾部窗口整理的纯逻辑与写回行为。"""

import json
import os
import sys
from collections import OrderedDict
from pathlib import Path

import pytest

# 设置项目根目录
project_root = Path(__file__).parent.parent
os.chdir(project_root)
sys.path.insert(0, str(project_root))

try:
    import engine.agent_files as agent_files_module
    import memory.consolidator as consolidator_module
    import memory.parser as file_ops_module
    from memory.consolidator import MemoryConsolidator
except ModuleNotFoundError as exc:
    pytest.skip(f"skip consolidator tests: missing dependency ({exc})", allow_module_level=True)


def _make_character_path(tmp_path: Path):
    def _path(name: str, subpath: str | None = None) -> str:
        base = tmp_path / name
        if subpath:
            return str(base / subpath)
        return str(base)

    return _path


def _render_memory_file(agent_name: str, sections: OrderedDict[str, str]) -> str:
    parts = [f"# {agent_name} 的长期记忆", ""]
    for date, body in sections.items():
        parts.append(f"## {date}")
        parts.append(body.strip())
        parts.append("")
    return "\n".join(parts).strip() + "\n"


def _event(date: str, slot: str, content: str) -> str:
    return (
        f"- **时间**：{date} {slot}\n"
        "- **地点**：公司\n"
        "- **在场**：我、他\n"
        f"- **内容**：{content}"
    )


def test_resolve_tail_start_uses_block_fingerprint():
    sections = OrderedDict(
        {
            "10月6日": "\n\n".join(
                [
                    _event("10月6日", "上午", "早上见面。"),
                    _event("10月6日", "中午", "一起吃饭。"),
                    _event("10月6日", "下午", "一起开会。"),
                ]
            )
        }
    )

    blocks = consolidator_module._flatten_sections(sections)
    start_index, skip_reason = consolidator_module._resolve_window_start(
        "chenxiao",
        blocks,
        _render_memory_file("chenxiao", sections),
        {"last_consolidated_block_id": blocks[1]["fingerprint"]},
    )

    assert skip_reason is None
    assert start_index == 1


def test_resolve_tail_start_migrates_from_last_memory_size():
    previous_sections = OrderedDict(
        {
            "10月6日": "\n\n".join(
                [
                    _event("10月6日", "上午", "早上见面。"),
                    _event("10月6日", "中午", "一起吃饭。"),
                ]
            )
        }
    )
    current_sections = OrderedDict(
        {
            "10月6日": "\n\n".join(
                [
                    _event("10月6日", "上午", "早上见面。"),
                    _event("10月6日", "中午", "一起吃饭。"),
                    _event("10月6日", "下午", "一起开会。"),
                ]
            )
        }
    )

    blocks = consolidator_module._flatten_sections(current_sections)
    start_index, skip_reason = consolidator_module._resolve_window_start(
        "chenxiao",
        blocks,
        _render_memory_file("chenxiao", current_sections),
        {"last_memory_size": len(_render_memory_file("chenxiao", previous_sections))},
    )

    assert skip_reason is None
    assert start_index == 1


def test_resolve_tail_start_reprocesses_when_marker_misses_but_size_recovers():
    sections = OrderedDict(
        {
            "10月6日": "\n\n".join(
                [
                    _event("10月6日", "上午", "早上见面。"),
                    _event("10月6日", "中午", "一起吃饭。"),
                ]
            )
        }
    )
    original_content = _render_memory_file("chenxiao", sections)
    blocks = consolidator_module._flatten_sections(sections)
    start_index, skip_reason = consolidator_module._resolve_window_start(
        "chenxiao",
        blocks,
        original_content,
        {
            "last_consolidated_block_id": "missing-marker",
            "last_memory_size": len(original_content),
        },
    )

    assert skip_reason is None
    assert start_index == 1


def test_resolve_tail_start_skips_when_boundary_invalid_and_memory_not_grown():
    sections = OrderedDict(
        {"10月6日": _event("10月6日", "上午", "早上见面。")}
    )
    original_content = _render_memory_file("chenxiao", sections)
    blocks = consolidator_module._flatten_sections(sections)
    start_index, skip_reason = consolidator_module._resolve_window_start(
        "chenxiao",
        blocks,
        original_content,
        {
            "last_consolidated_block_id": "missing-marker",
            "last_memory_size": len(original_content) + 1,
        },
    )

    assert start_index is None
    assert skip_reason == "memory 未增长，跳过本轮整理"


def test_validate_step1_dates_rejects_missing_window_date():
    error = consolidator_module._validate_step1_result(
        ["10月6日", "10月7日"],
        OrderedDict({"10月7日": _event("10月7日", "下午", "只返回了一天。")}),
    )

    assert error is not None
    assert "10月6日" in error
    assert "10月7日" in error


def test_normalize_adds_missing_event_bullets():
    normalized = file_ops_module.normalize(
        "\n".join(
            [
                "## 10月6日",
                "- **时间**：10月6日 晚上",
                "**地点**：卧室",
                "**在场**：我、他",
                "**关键词**：卧室 深夜 对视",
                "**重要度**：4",
                "**内容**：测试内容。",
            ]
        )
    )

    assert "- **地点**：卧室" in normalized
    assert "- **在场**：我、他" in normalized
    assert "- **关键词**：卧室 深夜 对视" in normalized
    assert "- **重要度**：4" in normalized
    assert "- **内容**：测试内容。" in normalized


def test_parse_step1_memories_accepts_non_bulleted_time_field():
    sections = consolidator_module._parse_step1_memories(
        "\n".join(
            [
                "**时间**：10月6日 19:20",
                "**地点**：日式烧鸟店门口",
                "**在场**：我、他",
                "**内容**：测试内容。",
            ]
        ),
        ["10月6日"],
    )

    assert list(sections.keys()) == ["10月6日"]
    assert sections["10月6日"].startswith("- **时间**：10月6日 19:20")
    assert "- **地点**：日式烧鸟店门口" in sections["10月6日"]
    assert "- **在场**：我、他" in sections["10月6日"]
    assert "- **内容**：测试内容。" in sections["10月6日"]


def test_parse_step1_memories_infers_single_window_date():
    sections = consolidator_module._parse_step1_memories(
        "\n".join(
            [
                "**时间**：19:20",
                "**地点**：日式烧鸟店门口",
                "**在场**：我、他",
                "**内容**：测试内容。",
            ]
        ),
        ["10月6日"],
    )

    assert list(sections.keys()) == ["10月6日"]
    assert sections["10月6日"].startswith("- **时间**：10月6日 19:20")


def test_build_consolidation_prompt_step1_5_contains_batched_chunks():
    consolidator = MemoryConsolidator()
    step1_markdown = "\n".join(
        [
            "## 10月6日",
            _event("10月6日", "19:20", "第一次靠近。"),
            "",
            _event("10月6日", "22:10", "分别前对视了很久。"),
        ]
    )

    system, user = consolidator._build_consolidation_prompt_step1_5(step1_markdown)

    assert "chunk" in system.lower()
    assert "<consolidated_memory>" in user
    assert "第一次靠近。" in user
    assert "分别前对视了很久。" in user


def test_parse_step1_5_metadata_and_apply_to_chunks():
    blocks = consolidator_module._flatten_sections(
        OrderedDict(
            {
                "10月6日": "\n\n".join(
                    [
                        _event("10月6日", "19:20", "第一次靠近。"),
                        _event("10月6日", "22:10", "分别前对视了很久。"),
                    ]
                )
            }
        )
    )

    llm_result = """
<chunk_meta>
时间：10月6日 19:20
keywords：公司 初次靠近 心动 紧张
importance：4
</chunk_meta>

<chunk_meta>
时间：10月6日 22:10
keywords：公司 对视 告别 不舍
    importance：3
</chunk_meta>
""".strip()

    metadata_items = consolidator_module._parse_step1_5_metadata(llm_result)
    merged = consolidator_module._merge_chunk_metadata(blocks, metadata_items)

    assert len(metadata_items) == 2
    assert "- **关键词**：公司 初次靠近 心动 紧张" in merged[0]["content"]
    assert "- **重要度**：4" in merged[0]["content"]
    assert "- **关键词**：公司 对视 告别 不舍" in merged[1]["content"]
    assert "- **重要度**：3" in merged[1]["content"]


@pytest.mark.asyncio
async def test_consolidate_agent_only_replaces_tail_window(tmp_path, monkeypatch):
    consolidator = MemoryConsolidator()
    agent_name = "chenxiao"
    path_helper = _make_character_path(tmp_path)

    monkeypatch.setattr(consolidator_module, "character_path", path_helper)
    monkeypatch.setattr(agent_files_module, "character_path", path_helper)
    monkeypatch.setattr(
        consolidator_module,
        "format_raw_dialogue_for_owner",
        lambda _agent_name, _limit: "旁白：测试场景",
    )
    monkeypatch.setattr(consolidator_module, "backup_file", lambda *_args, **_kwargs: None)

    async def fake_profile(_self, _agent_name):
        return 0, 0

    monkeypatch.setattr(MemoryConsolidator, "_consolidate_player_profile", fake_profile)

    original_sections = OrderedDict(
        {
            "10月6日": "\n\n".join(
                [
                    _event("10月6日", "上午", "上午稳定内容。"),
                    _event("10月6日", "中午", "中午旧内容。"),
                    _event("10月6日", "下午", "下午旧内容。"),
                ]
            )
        }
    )
    rewritten_sections = OrderedDict(
        {
            "10月6日": "\n\n".join(
                [
                    _event("10月6日", "中午", "中午新内容。"),
                    _event("10月6日", "下午", "下午新内容。"),
                ]
            )
        }
    )

    original_blocks = consolidator_module._flatten_sections(original_sections)
    rewritten_blocks = consolidator_module._flatten_sections(rewritten_sections)

    agent_dir = tmp_path / agent_name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "memory.md").write_text(
        _render_memory_file(agent_name, original_sections),
        encoding="utf-8",
    )
    (agent_dir / ".consolidation_state.json").write_text(
        json.dumps(
            {"last_consolidated_block_id": original_blocks[1]["fingerprint"]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    async def fake_run_memory_pipeline(
        _self,
        _agent_name: str,
        memory_entries: str,
        window_dates: list[str],
        raw_dialogue: str = "",
    ):
        assert "上午稳定内容" not in memory_entries
        assert "中午旧内容" in memory_entries
        assert "下午旧内容" in memory_entries
        assert window_dates == ["10月6日"]
        assert raw_dialogue == "旁白：测试场景"
        return rewritten_blocks, []

    monkeypatch.setattr(MemoryConsolidator, "_run_memory_pipeline", fake_run_memory_pipeline)

    vector_calls: list[tuple[str, str, list[str]]] = []

    async def fake_add(agent: str, date: str, chunks: list[str]) -> None:
        vector_calls.append((agent, date, chunks))

    monkeypatch.setattr(consolidator_module.vector_store, "add", fake_add)

    result = await consolidator.consolidate_agent(agent_name)
    memory_content = (agent_dir / "memory.md").read_text(encoding="utf-8")

    assert result is not None
    assert result.days == 1
    assert "上午稳定内容。" in memory_content
    assert "中午旧内容。" not in memory_content
    assert "下午旧内容。" not in memory_content
    assert "中午新内容。" in memory_content
    assert "下午新内容。" in memory_content
    assert vector_calls and vector_calls[0][0] == agent_name
    assert vector_calls[0][1] == "10月6日"


@pytest.mark.asyncio
async def test_consolidate_agent_writes_snapshot_size_in_state(tmp_path, monkeypatch):
    consolidator = MemoryConsolidator()
    agent_name = "chenxiao"
    path_helper = _make_character_path(tmp_path)

    monkeypatch.setattr(consolidator_module, "character_path", path_helper)
    monkeypatch.setattr(agent_files_module, "character_path", path_helper)
    monkeypatch.setattr(
        consolidator_module,
        "format_raw_dialogue_for_owner",
        lambda _agent_name, _limit: "旁白：测试场景",
    )
    monkeypatch.setattr(consolidator_module, "backup_file", lambda *_args, **_kwargs: None)

    async def fake_profile(_self, _agent_name):
        return 0, 0

    monkeypatch.setattr(MemoryConsolidator, "_consolidate_player_profile", fake_profile)

    sections = OrderedDict(
        {
            "10月6日": "\n\n".join(
                [
                    _event("10月6日", "上午", "上午内容。"),
                    _event("10月6日", "中午", "中午内容。"),
                ]
            )
        }
    )
    blocks = consolidator_module._flatten_sections(sections)

    agent_dir = tmp_path / agent_name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "memory.md").write_text(
        _render_memory_file(agent_name, sections),
        encoding="utf-8",
    )
    (agent_dir / ".consolidation_state.json").write_text(
        json.dumps(
            {"last_consolidated_block_id": blocks[0]["fingerprint"]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    async def fake_run_memory_pipeline(
        _self,
        _agent_name: str,
        memory_entries: str,
        window_dates: list[str],
        raw_dialogue: str = "",
    ):
        return blocks[1:], []

    monkeypatch.setattr(MemoryConsolidator, "_run_memory_pipeline", fake_run_memory_pipeline)
    monkeypatch.setattr(
        consolidator_module,
        "safe_write_memory",
        lambda *_args, **_kwargs: (999, 555),
    )

    async def fake_add(_agent: str, _date: str, _chunks: list[str]) -> None:
        return None

    monkeypatch.setattr(consolidator_module.vector_store, "add", fake_add)

    await consolidator.consolidate_agent(agent_name)
    state = json.loads((agent_dir / ".consolidation_state.json").read_text(encoding="utf-8"))

    assert state["last_memory_size"] == 555
