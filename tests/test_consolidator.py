"""测试尾部窗口整理的纯逻辑与写回行为。"""

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
    import storage.agent_files as agent_files_module
    import engine.consolidation_flow as consolidator_module
    import memory.parser as file_ops_module
    from engine.consolidation_flow import MemoryConsolidationFlow
    from engine.agent_schema import (
        MemoryMergeEvent,
        MemoryMergeOutput,
    )
    from memory.parser import parse_llm_memory_sections, split_into_events, is_structured_memory_block
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


def _consolidated_event(date: str, slot: str, content: str) -> str:
    return (
        f"- **时间**：{date} {slot}\n"
        "- **地点**：公司\n"
        "- **在场**：我、他\n"
        "- **关键词**：测试关键词\n"
        "- **重要度**：3\n"
        f"- **内容**：{content}"
    )


def test_prepare_window_detects_consolidated_boundary(monkeypatch):
    """已整理块（有关键词）之后的 raw 块应被识别为待整理窗口。"""
    monkeypatch.setattr(consolidator_module, "format_raw_dialogue_for_owner", lambda *_: "旁白：测试")
    consolidator = MemoryConsolidationFlow()
    sections = OrderedDict(
        {
            "10月6日": "\n\n".join(
                [
                    _consolidated_event("10月6日", "上午", "早上见面。"),
                    _event("10月6日", "中午", "一起吃饭。"),
                    _event("10月6日", "下午", "一起开会。"),
                ]
            )
        }
    )
    blocks = consolidator_module.flatten_sections(sections)
    window, skip_reason = consolidator._prepare_consolidation_window("chenxiao", blocks)

    assert skip_reason is None
    assert window is not None
    assert len(window.stable_blocks) == 1
    assert len(window.window_blocks) == 2
    assert "早上见面" not in window.window_memory_entries
    assert "一起吃饭" in window.window_memory_entries


def test_prepare_window_skips_when_no_participation(monkeypatch):
    """角色最近无发言时跳过，不看 memory 内容。"""
    monkeypatch.setattr(consolidator_module, "format_raw_dialogue_for_owner", lambda *_: "")
    consolidator = MemoryConsolidationFlow()
    sections = OrderedDict(
        {"10月6日": _event("10月6日", "上午", "早上见面。")}
    )
    blocks = consolidator_module.flatten_sections(sections)
    window, skip_reason = consolidator._prepare_consolidation_window("chenxiao", blocks)

    assert window is None
    assert "无参与" in skip_reason


def test_prepare_window_reprocesses_last_block_when_all_consolidated(monkeypatch):
    """全部块都已整理时，将最后一块纳入窗口重整理。"""
    monkeypatch.setattr(consolidator_module, "format_raw_dialogue_for_owner", lambda *_: "旁白：测试")
    consolidator = MemoryConsolidationFlow()
    sections = OrderedDict(
        {
            "10月6日": "\n\n".join(
                [
                    _consolidated_event("10月6日", "上午", "早上见面。"),
                    _consolidated_event("10月6日", "中午", "一起吃饭。"),
                ]
            )
        }
    )
    blocks = consolidator_module.flatten_sections(sections)
    window, skip_reason = consolidator._prepare_consolidation_window("chenxiao", blocks)

    assert skip_reason is None
    assert window is not None
    assert len(window.stable_blocks) == 1
    assert len(window.window_blocks) == 1
    assert "一起吃饭" in window.window_memory_entries


def test_prepare_window_processes_all_when_none_consolidated(monkeypatch):
    """没有任何已整理块时从头处理。"""
    monkeypatch.setattr(consolidator_module, "format_raw_dialogue_for_owner", lambda *_: "旁白：测试")
    consolidator = MemoryConsolidationFlow()
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
    blocks = consolidator_module.flatten_sections(sections)
    window, skip_reason = consolidator._prepare_consolidation_window("chenxiao", blocks)

    assert skip_reason is None
    assert window is not None
    assert len(window.stable_blocks) == 0
    assert len(window.window_blocks) == 2


def test_prepare_window_starts_from_first_invalid_block_even_if_later_blocks_are_structured(monkeypatch):
    monkeypatch.setattr(consolidator_module, "format_raw_dialogue_for_owner", lambda *_: "旁白：测试")
    consolidator = MemoryConsolidationFlow()
    sections = OrderedDict(
        {
            "10月17日": "\n\n".join(
                [
                    _consolidated_event("10月17日", "上午", "上午稳定内容。"),
                    "- **17:29/玩家住处客厅/我和玩家**：10月17日残留 raw。",
                ]
            ),
            "10月18日": _consolidated_event("10月18日", "上午", "后一天已整理内容。"),
        }
    )
    blocks = consolidator_module.flatten_sections(sections)
    window, skip_reason = consolidator._prepare_consolidation_window("chenxiao", blocks)

    assert skip_reason is None
    assert window is not None
    assert len(window.stable_blocks) == 1
    assert window.window_dates == ["10月17日", "10月18日"]
    assert "10月17日残留 raw" in window.window_memory_entries
    assert "后一天已整理内容" in window.window_memory_entries


def test_update_player_bootstraps_tmp_user_from_current_profile(tmp_path, monkeypatch):
    agent_name = "chenxiao"
    path_helper = _make_character_path(tmp_path)

    monkeypatch.setattr(agent_files_module, "character_path", path_helper)

    agent_dir = tmp_path / agent_name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "user.md").write_text(
        "\n".join(
            [
                "# 角色眼中的玩家",
                "",
                "## 基本信息",
                "- 姓名：李小明",
                "",
                "## 对方是什么人",
                "（暂无）",
                "",
                "## 我们怎么相处",
                "（暂无）",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = agent_files_module.update_player(agent_name, "对方是什么人", "- 很直接")
    tmp_content = (agent_dir / "tmp_user.md").read_text(encoding="utf-8")

    assert result == {
        "file": "tmp_user.md",
        "target": "对方是什么人",
        "operation": "append",
        "appended": "- 很直接",
    }
    assert "## 基本信息" in tmp_content
    assert "- 姓名：李小明" in tmp_content
    assert "## 对方是什么人" in tmp_content
    assert "- 很直接" in tmp_content
    assert "## 我们怎么相处" in tmp_content
    assert "（暂无）" in tmp_content


def test_update_player_keeps_existing_tmp_user_draft(tmp_path, monkeypatch):
    agent_name = "chenxiao"
    path_helper = _make_character_path(tmp_path)

    monkeypatch.setattr(agent_files_module, "character_path", path_helper)

    agent_dir = tmp_path / agent_name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "user.md").write_text(
        "\n".join(
            [
                "# 角色眼中的玩家",
                "",
                "## 基本信息",
                "- 姓名：李小明",
                "",
                "## 对方是什么人",
                "（暂无）",
                "",
                "## 我们怎么相处",
                "（暂无）",
                "",
            ]
        ),
        encoding="utf-8",
    )

    agent_files_module.update_player(agent_name, "对方是什么人", "- 很直接")
    agent_files_module.update_player(agent_name, "对方是什么人", "- 很细心")
    tmp_content = (agent_dir / "tmp_user.md").read_text(encoding="utf-8")

    assert tmp_content.count("## 对方是什么人") == 1
    assert "- 很直接" in tmp_content
    assert "- 很细心" in tmp_content


def test_agent_file_updates_return_structured_json_items(tmp_path, monkeypatch):
    agent_name = "chenxiao"
    path_helper = _make_character_path(tmp_path)

    monkeypatch.setattr(agent_files_module, "character_path", path_helper)

    agent_dir = tmp_path / agent_name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "status.md").write_text(
        "\n".join(
            [
                "# 我的状态",
                "",
                "## 场景",
                "旧教学楼走廊",
                "",
                "## 打算",
                "- [ ] 【去天台】午休去天台找玩家",
                "",
            ]
        ),
        encoding="utf-8",
    )

    replace_result = agent_files_module.update_status(agent_name, "场景", "图书馆二楼靠窗座位")
    assert replace_result == {
        "file": "status.md",
        "target": "场景",
        "operation": "replace",
        "before": "旧教学楼走廊",
        "after": "图书馆二楼靠窗座位",
    }

    add_result = agent_files_module.add_pending_event(agent_name, "【新计划】去图书馆", "打算")
    assert add_result == {
        "file": "status.md",
        "target": "打算",
        "operation": "add",
        "added": "- [ ] 【新计划】去图书馆",
    }

    skip_result = agent_files_module.add_pending_event(agent_name, "【新计划】去图书馆", "打算")
    assert skip_result == {
        "file": "status.md",
        "target": "打算",
        "operation": "skip",
        "reason": "【新计划】已存在，跳过",
    }

    remove_result = agent_files_module.mark_event_triggered(agent_name, "去天台", "打算")
    assert remove_result == {
        "file": "status.md",
        "target": "打算",
        "operation": "remove",
        "removed": "- [ ] 【去天台】午休去天台找玩家",
    }

    memory_entry = (
        "- **时间**：10月24日 上午\n"
        "- **地点**：图书馆\n"
        "- **在场**：我、玩家\n"
        "- **内容**：玩家主动替我解围。"
    )
    append_result = agent_files_module.update_memory(agent_name, memory_entry)
    appended_memory = "\n\n".join(memory_entry.split("\n"))
    assert append_result == {
        "file": "memory.md",
        "target": "长期记忆",
        "operation": "append",
        "appended": appended_memory,
    }

    duplicate_result = agent_files_module.update_memory(agent_name, memory_entry)
    assert duplicate_result == {
        "file": "memory.md",
        "target": "长期记忆",
        "operation": "skip",
        "reason": "所有 entry 已存在，跳过",
    }


@pytest.mark.asyncio
async def test_consolidate_player_profile_uses_single_draft_profile(tmp_path, monkeypatch):
    consolidator = MemoryConsolidationFlow()
    agent_name = "chenxiao"
    path_helper = _make_character_path(tmp_path)

    monkeypatch.setattr(consolidator_module, "character_path", path_helper)
    monkeypatch.setattr(agent_files_module, "character_path", path_helper)
    monkeypatch.setattr(consolidator_module, "backup_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(consolidator_module, "get_player_profile_agent", lambda: object())
    monkeypatch.setattr(
        consolidator_module,
        "get_consolidation_llm_config",
        lambda temperature=None: {"model": "test-model"},
    )

    captured: dict[str, object] = {}

    async def fake_run_text_agent(
        *,
        agent,
        user_input,
        **kwargs,
    ):
        captured["agent"] = agent
        captured["user"] = user_input
        assert kwargs["usage_phase"] == "consolidation.player_profile"
        return "\n".join(
            [
                "# 角色眼中的玩家",
                "",
                "## 基本信息",
                "- 姓名：李小明",
                "",
                "## 对方是什么人",
                "- 稳定判断",
                "",
                "## 我们怎么相处",
                "- 稳定互动",
                "",
            ]
        )

    monkeypatch.setattr(consolidator_module, "run_text_agent", fake_run_text_agent)

    agent_dir = tmp_path / agent_name
    agent_dir.mkdir(parents=True, exist_ok=True)
    user_content = "\n".join(
        [
            "# 角色眼中的玩家",
            "",
            "## 基本信息",
            "- 姓名：李小明",
            "",
            "## 对方是什么人",
            "- 原有判断",
            "",
            "## 我们怎么相处",
            "- 原有互动",
            "",
        ]
    )
    draft_content = "\n".join(
        [
            "# 角色眼中的玩家",
            "",
            "## 基本信息",
            "- 姓名：李小明",
            "",
            "## 对方是什么人",
            "- 原有判断",
            "- 新增判断",
            "",
            "## 我们怎么相处",
            "- 原有互动",
            "",
        ]
    )
    (agent_dir / "user.md").write_text(user_content, encoding="utf-8")
    (agent_dir / "tmp_user.md").write_text(draft_content, encoding="utf-8")

    before_len, after_len = await consolidator._consolidate_player_profile(agent_name)
    draft_input = captured["user"]

    assert before_len == len(user_content)
    assert after_len > 0
    assert "<draft_profile>" in draft_input
    assert "<current_profile>" not in draft_input
    assert "<staged_updates>" not in draft_input
    assert "- 原有判断" in draft_input
    assert "- 新增判断" in draft_input
    assert (agent_dir / "user.md").read_text(encoding="utf-8").strip().endswith("- 稳定互动")
    assert not (agent_dir / "tmp_user.md").exists()


def test_enforce_user_section_limits_preserves_custom_sections():
    existing_content = "\n".join(
        [
            "# 角色眼中的玩家",
            "",
            "## 基本信息",
            "- 姓名：李小明",
            "",
            "## 特殊雷区",
            "- 讨厌被突然碰手腕",
            "",
            "## 我们怎么相处",
            "- 会先观察我的情绪",
            "",
        ]
    )

    rendered = consolidator_module._enforce_user_section_limits(existing_content)

    assert "## 特殊雷区" in rendered
    assert "- 讨厌被突然碰手腕" in rendered
    assert rendered.count("## 特殊雷区") == 1


@pytest.mark.asyncio
async def test_merge_memory_blocks_uses_factory_agent_getter(monkeypatch):
    consolidator = MemoryConsolidationFlow()
    sentinel_agent = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(consolidator_module, "get_memory_merge_agent", lambda: sentinel_agent)

    async def fake_run_consolidation_agent(
        self_inner,
        *,
        agent,
        output_type,
        agent_name,
        function_name,
        user,
    ):
        captured["agent"] = agent
        captured["user"] = user
        assert output_type is MemoryMergeOutput
        assert agent_name == "chenxiao"
        assert function_name == "memory_merge"
        return MemoryMergeOutput(
            events=[
                MemoryMergeEvent(
                    date="10月19日",
                    time="10月19日 晚上",
                    location="餐厅",
                    participants="我、他",
                    content="一起吃饭。",
                )
            ]
        )

    monkeypatch.setattr(
        consolidator_module.MemoryConsolidationFlow,
        "_run_consolidation_agent",
        fake_run_consolidation_agent,
    )

    blocks, merged_markdown = await consolidator._merge_memory_blocks(
        "chenxiao",
        "payload",
        "raw",
        ["10月19日"],
    )

    assert captured["agent"] is sentinel_agent
    assert "<memory_entries>" in captured["user"]
    assert len(blocks) == 1
    assert "一起吃饭。" in merged_markdown


def test_enforce_user_section_limits_trims_to_configured_caps():
    content = "\n".join(
        [
            "# 角色眼中的玩家",
            "",
            "## 基本信息",
            "- 姓名：李小明",
            "",
            "## 对方是什么人",
            *[f"- 判断{i}" for i in range(1, 11)],
            "",
            "## 我们怎么相处",
            *[f"- 相处{i}" for i in range(1, 8)],
            "",
        ]
    )

    trimmed = consolidator_module._enforce_user_section_limits(content)

    assert trimmed.count("- 判断") == 8
    assert trimmed.count("- 相处") == 5
    assert "- 判断9" not in trimmed
    assert "- 相处6" not in trimmed


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


def test_split_into_events_uses_blank_lines_between_blocks():
    day_content = "\n\n".join(
        [
            _consolidated_event("10月6日", "上午", "上午稳定内容。"),
            "- **17:29/玩家住处客厅/我和玩家**：这是 raw 条目。",
        ]
    )

    blocks = split_into_events(day_content)

    assert len(blocks) == 2
    assert "上午稳定内容。" in blocks[0]
    assert "17:29/玩家住处客厅/我和玩家" in blocks[1]


def test_structured_memory_block_rejects_trailing_raw_text():
    legal_block = _consolidated_event("10月6日", "上午", "上午稳定内容。")
    mixed_block = "\n".join(
        [
            legal_block,
            "- **17:29/玩家住处客厅/我和玩家**：这是被吞进块里的 raw 条目。",
        ]
    )

    assert is_structured_memory_block(legal_block) is True
    assert is_structured_memory_block(mixed_block) is False


def test_split_by_date_preserves_blank_line_when_same_date_repeats():
    normalized = file_ops_module.normalize(
        "\n".join(
            [
                "## 10月18日",
                _consolidated_event("10月18日", "中午", "第一块。"),
                "",
                "## 10月18日 18:06",
                "",
                "- **18:06/创意部工位区/我和玩家**：第二块 raw。",
            ]
        )
    )

    sections = file_ops_module.split_by_date(normalized)
    blocks = split_into_events(sections["10月18日"])

    assert len(blocks) == 2
    assert "第一块。" in blocks[0]
    assert "第二块 raw。" in blocks[1]


def test_parse_memory_merge_output_accepts_non_bulleted_time_field():
    sections = parse_llm_memory_sections(
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


def test_parse_memory_merge_output_accepts_plain_fields_without_bold_markup():
    sections = parse_llm_memory_sections(
        "\n".join(
            [
                "时间：10月21日 09:42-09:45",
                "地点：玩家家卧室及客厅玄关",
                "在场：我、他",
                "内容：他抱着我提议去吃栗子蛋糕，我换上裙子后和他一起出门。",
            ]
        ),
        ["10月21日"],
    )

    assert list(sections.keys()) == ["10月21日"]
    assert sections["10月21日"].startswith("- **时间**：10月21日 09:42-09:45")
    assert "- **地点**：玩家家卧室及客厅玄关" in sections["10月21日"]
    assert "- **在场**：我、他" in sections["10月21日"]
    assert "- **内容**：他抱着我提议去吃栗子蛋糕，我换上裙子后和他一起出门。" in sections["10月21日"]


def test_parse_memory_merge_output_infers_single_window_date():
    sections = parse_llm_memory_sections(
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


def test_is_structured_memory_block_accepts_plain_fields_without_bold_markup():
    assert is_structured_memory_block(
        "\n".join(
            [
                "时间：10月21日 09:42-09:45",
                "地点：玩家家卧室及客厅玄关",
                "在场：我、他",
                "关键词：栗子蛋糕 出门 换裙子",
                "重要度：3",
                "内容：他抱着我聊天，后来带我出门吃栗子蛋糕。",
            ]
        )
    )


def test_validate_memory_merge_output_rejects_missing_dates():
    events = [
        MemoryMergeEvent(
            date="10月19日", time="10月19日 晚上", location="餐厅",
            participants="我、他", content="只输出了最后一天。"
        )
    ]

    error = consolidator_module._validate_memory_merge_output(
        events,
        window_dates=["10月9日", "10月19日"],
    )

    assert error == "输出缺少日期：10月9日"


def test_merge_chunk_metadata_applies_structured_items():
    blocks = consolidator_module.flatten_sections(
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

    metadata_items = [
        {"time": "10月6日 19:20", "keywords": ["公司", "初次靠近", "心动", "紧张"], "importance": 4},
        {"time": "10月6日 22:10", "keywords": ["公司", "对视", "告别", "不舍"], "importance": 3},
    ]

    merged = consolidator_module._merge_chunk_metadata(blocks, metadata_items)

    assert len(merged) == 2
    assert "- **关键词**：公司 初次靠近 心动 紧张" in merged[0]["content"]
    assert "- **重要度**：4" in merged[0]["content"]
    assert "- **关键词**：公司 对视 告别 不舍" in merged[1]["content"]
    assert "- **重要度**：3" in merged[1]["content"]


@pytest.mark.asyncio
async def test_consolidate_agent_only_replaces_tail_window(tmp_path, monkeypatch):
    consolidator = MemoryConsolidationFlow()
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

    monkeypatch.setattr(MemoryConsolidationFlow, "_consolidate_player_profile", fake_profile)

    original_sections = OrderedDict(
        {
            "10月6日": "\n\n".join(
                [
                    _consolidated_event("10月6日", "上午", "上午稳定内容。"),
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
                    _consolidated_event("10月6日", "中午", "中午新内容。"),
                    _consolidated_event("10月6日", "下午", "下午新内容。"),
                ]
            )
        }
    )

    rewritten_blocks = consolidator_module.flatten_sections(rewritten_sections)

    agent_dir = tmp_path / agent_name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "memory.md").write_text(
        _render_memory_file(agent_name, original_sections),
        encoding="utf-8",
    )

    async def fake_apply_consolidation_pipeline(
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

    monkeypatch.setattr(
        MemoryConsolidationFlow,
        "_apply_consolidation_pipeline",
        fake_apply_consolidation_pipeline,
    )

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
