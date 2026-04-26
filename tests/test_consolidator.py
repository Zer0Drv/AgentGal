"""测试尾部窗口整理的纯逻辑与写回行为。"""

import os
import sys
from pathlib import Path

import pytest

# 设置项目根目录
project_root = Path(__file__).parent.parent
os.chdir(project_root)
sys.path.insert(0, str(project_root))

try:
    import storage.agent_files as agent_files_module
    import consolidation.flow as consolidator_module
    import memory.parser as parser_module
    from consolidation.flow import MemoryConsolidationFlow
    from agents.schema import EpisodeMemoryBlock
    from memory.parser import (
        EpisodeMemory,
        append_memory_records,
        read_memory_jsonl,
    )
except ModuleNotFoundError as exc:
    pytest.skip(f"skip consolidator tests: missing dependency ({exc})", allow_module_level=True)


def _make_character_path(tmp_path: Path):
    def _path(name: str, subpath: str | None = None) -> str:
        base = tmp_path / name
        if subpath:
            return str(base / subpath)
        return str(base)

    return _path


def _event(date: str, slot: str, content: str) -> str:
    return (
        f"- **时间**：{date} {slot}\n"
        "- **地点**：公司\n"
        "- **在场**：我、他\n"
        f"- **内容**：{content}"
    )


def test_prepare_slice_returns_none_when_draft_empty(tmp_path, monkeypatch):
    """memory_draft.jsonl 不存在或切片为空时返回 None。"""
    monkeypatch.setattr(consolidator_module, "character_path", _make_character_path(tmp_path))
    monkeypatch.setattr(agent_files_module, "character_path", _make_character_path(tmp_path))
    consolidator = MemoryConsolidationFlow()

    result = consolidator._prepare_consolidation_slice("chenxiao", until_turn=5, raw_messages=[])
    assert result is None


def test_prepare_slice_skips_entries_beyond_until_turn(tmp_path, monkeypatch):
    """只取 turn <= until_turn 的 draft 条目，后续 turn 留在 remaining。"""
    monkeypatch.setattr(consolidator_module, "character_path", _make_character_path(tmp_path))
    monkeypatch.setattr(agent_files_module, "character_path", _make_character_path(tmp_path))

    agent_files_module.append_memory_draft("chenxiao", 3, "- 第三轮 draft")
    agent_files_module.append_memory_draft("chenxiao", 5, "- 第五轮 draft")

    raw_messages = [
        {"role": "narrator", "content": "场景 A", "visible_to": ["chenxiao", "narrator"], "turn": 3},
        {"role": "chenxiao", "content": "回应 A", "visible_to": ["chenxiao", "narrator"], "turn": 3},
    ]
    consolidator = MemoryConsolidationFlow()
    result = consolidator._prepare_consolidation_slice(
        "chenxiao", until_turn=3, raw_messages=raw_messages
    )

    assert result is not None
    taken, remaining, memory_entries, raw_dialogue = result
    assert [r["turn"] for r in taken] == [3]
    assert [r["turn"] for r in remaining] == [5]
    assert "第三轮 draft" in memory_entries
    assert "第五轮 draft" not in memory_entries
    assert "[turn=3]" in raw_dialogue


def test_prepare_slice_returns_none_when_no_entries_within_turn(tmp_path, monkeypatch):
    """Draft 非空但所有条目 turn 都大于 until_turn 时跳过。"""
    monkeypatch.setattr(consolidator_module, "character_path", _make_character_path(tmp_path))
    monkeypatch.setattr(agent_files_module, "character_path", _make_character_path(tmp_path))
    agent_files_module.append_memory_draft("chenxiao", 10, "- 未来轮 draft")

    consolidator = MemoryConsolidationFlow()
    result = consolidator._prepare_consolidation_slice(
        "chenxiao", until_turn=5, raw_messages=[]
    )

    assert result is None


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
async def test_merge_memory_blocks_uses_episode_memory_generator(monkeypatch):
    consolidator = MemoryConsolidationFlow()
    sentinel_agent = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        consolidator_module,
        "get_episode_memory_generator_agent",
        lambda: sentinel_agent,
    )

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
        assert output_type is EpisodeMemoryBlock
        assert agent_name == "chenxiao"
        assert function_name == "episode_memory_generator"
        return EpisodeMemoryBlock(
            date="10月19日",
            time="10月19日 晚上",
            location="餐厅",
            participants="我、他",
            keywords=["餐厅", "吃饭", "日常"],
            importance=2,
            content="一起吃饭。",
            title="餐厅晚饭",
        )

    monkeypatch.setattr(
        consolidator_module.MemoryConsolidationFlow,
        "_run_consolidation_agent",
        fake_run_consolidation_agent,
    )

    episode = await consolidator._merge_memory_blocks(
        "chenxiao",
        "payload",
        "raw 对话原文",
    )

    assert captured["agent"] is sentinel_agent
    assert "<memory_entries>" in captured["user"]
    assert episode.date == "10月19日"
    assert episode.content == "一起吃饭。"
    assert episode.location == "餐厅"
    assert episode.memory_owner == "chenxiao"
    assert episode.keywords == ["餐厅", "吃饭", "日常"]
    assert episode.importance == 2
    assert episode.title == "餐厅晚饭"
    # raw_dialogue 由流程注入，不由 LLM 输出
    assert episode.raw_dialogue == "raw 对话原文"


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


@pytest.mark.asyncio
async def test_consolidate_agent_merges_draft_into_memory_and_clears_draft(tmp_path, monkeypatch):
    consolidator = MemoryConsolidationFlow()
    agent_name = "chenxiao"
    path_helper = _make_character_path(tmp_path)

    monkeypatch.setattr(consolidator_module, "character_path", path_helper)
    monkeypatch.setattr(agent_files_module, "character_path", path_helper)
    monkeypatch.setattr(parser_module, "character_path", path_helper)
    monkeypatch.setattr(
        consolidator_module,
        "load_conversation_history",
        lambda limit=None: [
            {
                "role": "narrator",
                "content": "旁白：测试场景",
                "visible_to": [agent_name, "narrator"],
                "turn": 4,
            },
            {
                "role": agent_name,
                "content": "角色回应",
                "visible_to": [agent_name, "narrator"],
                "turn": 4,
            },
        ],
    )
    monkeypatch.setattr(consolidator_module, "backup_file", lambda *_args, **_kwargs: None)

    async def fake_profile(_self, _agent_name):
        return 0, 0

    monkeypatch.setattr(MemoryConsolidationFlow, "_consolidate_player_profile", fake_profile)

    agent_dir = tmp_path / agent_name
    agent_dir.mkdir(parents=True, exist_ok=True)

    # 已有一条存量记忆（日期 10月6日 上午），append-only 流程应保留
    seed_record = EpisodeMemory(
        date="10月6日",
        time="10月6日 上午",
        location="公司",
        participants="我、他",
        keywords=["公司", "日常"],
        importance=2,
        content="上午稳定内容。",
    )
    append_memory_records(agent_name, [seed_record])

    # draft 写入 memory_draft.jsonl，带 turn 标记
    agent_files_module.append_memory_draft(agent_name, 3, _event("10月6日", "中午", "中午 draft 内容。"))
    agent_files_module.append_memory_draft(agent_name, 4, _event("10月6日", "下午", "下午 draft 内容。"))
    # 模拟还未闭合的下一轮 draft（turn=5），不应被本次归并
    agent_files_module.append_memory_draft(agent_name, 5, _event("10月6日", "傍晚", "傍晚 draft 内容。"))

    rewritten_episode = EpisodeMemory(
        date="10月6日",
        time="10月6日 中午",
        location="公司",
        participants="我、他",
        keywords=["公司", "合并"],
        importance=3,
        content="中午下午合并后内容。",
    )

    async def fake_apply_consolidation_pipeline(
        _self,
        _agent_name: str,
        memory_entries: str,
        raw_dialogue: str = "",
    ):
        assert "中午 draft 内容" in memory_entries
        assert "下午 draft 内容" in memory_entries
        assert "傍晚 draft 内容" not in memory_entries
        assert "上午稳定内容" not in memory_entries
        assert "[turn=4]" in raw_dialogue
        return rewritten_episode, []

    monkeypatch.setattr(
        MemoryConsolidationFlow,
        "_apply_consolidation_pipeline",
        fake_apply_consolidation_pipeline,
    )

    vector_calls: list[EpisodeMemory] = []

    async def fake_add(episode: EpisodeMemory) -> None:
        vector_calls.append(episode)

    monkeypatch.setattr(consolidator_module.vector_store, "add", fake_add)

    result = await consolidator.consolidate_agent(agent_name, until_turn=4)

    assert result is not None
    assert result.days == 1
    assert result.date_range == "10月6日~10月6日"

    # append-only: 存量 1 条 + 新增 1 条 = 2 条，并按 append 顺序排列
    records = read_memory_jsonl(agent_name)
    assert len(records) == 2
    assert records[0].content == "上午稳定内容。"
    assert records[1].content == "中午下午合并后内容。"

    # 未闭合的 turn=5 draft 条目应保留
    remaining_draft = agent_files_module.read_memory_draft(agent_name)
    assert [r["turn"] for r in remaining_draft] == [5]
    assert "傍晚 draft 内容" in remaining_draft[0]["text"]

    # 向量索引只接收本次 append 的 1 条新记录
    assert len(vector_calls) == 1
    assert vector_calls[0].memory_owner == agent_name
    assert vector_calls[0].content == "中午下午合并后内容。"
    assert isinstance(vector_calls[0], EpisodeMemory)
    assert isinstance(vector_calls[0].keywords, list)
    assert 1 <= vector_calls[0].importance <= 5
