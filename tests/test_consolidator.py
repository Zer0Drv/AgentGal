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
    from agents.schema import EpisodeClosureOutput, EpisodeMemoryBlock, UnderstandingPatchOutput
    from memory.parser import (
        EpisodeMemory,
        Understanding,
        UnderstandingHistoryEntry,
        append_memory_records,
        read_memory_jsonl,
        read_understandings,
        write_understandings,
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
    monkeypatch.setattr(agent_files_module, "character_path", _make_character_path(tmp_path))
    consolidator = MemoryConsolidationFlow()

    result = consolidator._prepare_consolidation_slice("chenxiao", until_turn=5, raw_messages=[])
    assert result is None


def test_prepare_slice_skips_entries_beyond_until_turn(tmp_path, monkeypatch):
    """只取 turn <= until_turn 的 draft 条目，后续 turn 留在 remaining。"""
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
    monkeypatch.setattr(agent_files_module, "character_path", _make_character_path(tmp_path))
    agent_files_module.append_memory_draft("chenxiao", 10, "- 未来轮 draft")

    consolidator = MemoryConsolidationFlow()
    result = consolidator._prepare_consolidation_slice(
        "chenxiao", until_turn=5, raw_messages=[]
    )

    assert result is None


@pytest.mark.asyncio
async def test_detect_closures_does_not_close_latest_open_narrator_turn(monkeypatch):
    """最新 narrator turn 尚未等到下一条旁白，不能被归并。"""
    consolidator = MemoryConsolidationFlow()
    monkeypatch.setattr(
        consolidator_module,
        "get_episode_closure_detector_agent",
        lambda: object(),
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
        assert output_type is EpisodeClosureOutput
        assert function_name == "episode_closure_detector"
        assert "[turn=4]" in user
        return EpisodeClosureOutput.model_validate(
            {
                "chenxiao": [
                    {
                        "end_turn": 3,
                        "old_theme": "上一段互动",
                        "new_theme": "当前开放互动",
                        "reason": "turn 4 开始新互动",
                    },
                    {
                        "end_turn": 4,
                        "old_theme": "当前开放互动",
                        "new_theme": "还没有下一条旁白的误判",
                        "reason": "latest turn must remain open",
                    },
                ]
            }
        )

    monkeypatch.setattr(
        MemoryConsolidationFlow,
        "_run_consolidation_agent",
        fake_run_consolidation_agent,
    )

    raw_messages = [
        {"role": "narrator", "content": "旧场景", "turn": 3, "visible_to": ["chenxiao", "narrator"]},
        {"role": "chenxiao", "content": "旧回应", "turn": 3, "visible_to": ["chenxiao", "narrator"]},
        {"role": "narrator", "content": "新场景", "turn": 4, "visible_to": ["chenxiao", "narrator"]},
        {"role": "chenxiao", "content": "新回应", "turn": 4, "visible_to": ["chenxiao", "narrator"]},
    ]

    closures = await consolidator._detect_closures(
        ["chenxiao"],
        raw_messages,
        earliest_draft_turn=3,
        latest_open_turn=4,
    )

    assert closures == {"chenxiao": 3}


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


def test_apply_understanding_patch_updates_and_adds(monkeypatch):
    class FakeUUID:
        hex = "new-understanding-id"

    monkeypatch.setattr(consolidator_module.uuid, "uuid4", lambda: FakeUUID())

    existing = {
        "u1": Understanding(
            id="u1",
            memory_owner="chenxiao",
            subject="对玩家的认知",
            keywords=["玩家"],
            content="旧理解。",
            linked_episodes=["e0"],
            history=[
                UnderstandingHistoryEntry(
                    episode_id="e0",
                    date="10月18日",
                    title="旧事件",
                    content="旧理解。",
                )
            ],
        ),
    }
    patch = UnderstandingPatchOutput.model_validate(
        {
            "add": [
                {
                    "subject": "互动模式",
                    "keywords": ["靠近"],
                    "content": "玩家会用行动解释。",
                }
            ],
            "update": {
                " u1 ": {
                    "subject": "",
                    "keywords": ["玩家", "保护"],
                    "content": "玩家在压力下会先确认她是否安全。",
                }
            },
        }
    )

    result = consolidator_module._apply_understanding_patch(
        "chenxiao",
        existing,
        patch,
        EpisodeMemory(id="e1", date="10月19日", title="餐厅晚饭"),
    )

    assert result.logs == ["UPDATE u1", "ADD new-understanding-id"]
    assert result.updated["u1"].subject == "对玩家的认知"
    assert result.updated["u1"].keywords == ["玩家", "保护"]
    assert result.updated["u1"].linked_episodes == ["e0", "e1"]
    assert [entry.episode_id for entry in result.updated["u1"].history] == ["e0", "e1"]
    assert result.updated["u1"].history[-1].date == "10月19日"
    assert result.updated["u1"].history[-1].title == "餐厅晚饭"
    assert result.updated["u1"].history[-1].content == "玩家在压力下会先确认她是否安全。"
    assert result.updated["new-understanding-id"].memory_owner == "chenxiao"
    assert result.updated["new-understanding-id"].content == "玩家会用行动解释。"
    assert len(result.updated["new-understanding-id"].history) == 1
    assert result.updated["new-understanding-id"].history[0].episode_id == "e1"
    assert result.updated["new-understanding-id"].history[0].date == "10月19日"
    assert result.updated["new-understanding-id"].history[0].title == "餐厅晚饭"
    assert result.updated["new-understanding-id"].history[0].content == "玩家会用行动解释。"


def test_apply_understanding_patch_does_not_append_history_for_link_only_update():
    existing = {
        "u1": Understanding(
            id="u1",
            memory_owner="chenxiao",
            subject="对玩家的认知",
            keywords=["玩家"],
            content="玩家会认真履行约定。",
            linked_episodes=["e0"],
        ),
    }
    patch = UnderstandingPatchOutput.model_validate(
        {
            "update": {
                "u1": {
                    "content": "玩家会认真履行约定。",
                }
            }
        }
    )

    result = consolidator_module._apply_understanding_patch(
        "chenxiao",
        existing,
        patch,
        EpisodeMemory(id="e1", date="10月20日", title="再次确认"),
    )

    assert result.links_only_ids == ["u1"]
    assert result.updated["u1"].linked_episodes == ["e0", "e1"]
    assert result.updated["u1"].history == []


@pytest.mark.asyncio
async def test_apply_pipeline_assigns_episode_id_before_understanding_patch(monkeypatch):
    class FakeUUID:
        hex = "episode-id"

    consolidator = MemoryConsolidationFlow()
    captured: dict[str, str] = {}

    async def fake_merge(_self, _agent_name, _memory_entries, _raw_dialogue):
        return EpisodeMemory(
            date="10月19日",
            time="10月19日 晚上",
            location="餐厅",
            participants="我、他",
            content="一起吃饭。",
            memory_owner="chenxiao",
        )

    async def fake_understanding(_self, _agent_name, episode):
        captured["id"] = episode.id

    monkeypatch.setattr(consolidator_module.uuid, "uuid4", lambda: FakeUUID())
    monkeypatch.setattr(MemoryConsolidationFlow, "_merge_memory_blocks", fake_merge)
    monkeypatch.setattr(
        MemoryConsolidationFlow, "_patch_understandings", fake_understanding
    )

    episode, errors = await consolidator._apply_consolidation_pipeline(
        "chenxiao", "draft", "raw"
    )

    assert errors == []
    assert episode is not None
    assert episode.id == "episode-id"
    assert captured["id"] == "episode-id"


@pytest.mark.asyncio
async def test_patch_understandings_writes_file_and_syncs_vectors(tmp_path, monkeypatch):
    class FakeUUID:
        hex = "new-understanding-id"

    consolidator = MemoryConsolidationFlow()
    agent_name = "chenxiao"
    path_helper = _make_character_path(tmp_path)
    sentinel_agent = object()

    monkeypatch.setattr(consolidator_module.uuid, "uuid4", lambda: FakeUUID())
    monkeypatch.setattr(parser_module, "character_path", path_helper)
    monkeypatch.setattr(agent_files_module, "character_path", path_helper)
    monkeypatch.setattr(
        consolidator_module,
        "get_understanding_patch_agent",
        lambda: sentinel_agent,
    )

    agent_dir = tmp_path / agent_name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "soul.md").write_text("<identity>陈晓</identity>", encoding="utf-8")
    write_understandings(
        agent_name,
        {
            "u1": Understanding(
                id="u1",
                memory_owner=agent_name,
                subject="对玩家的认知",
                keywords=["玩家"],
                content="旧理解。",
                linked_episodes=["e0"],
            )
        },
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
        assert agent is sentinel_agent
        assert output_type is UnderstandingPatchOutput
        assert agent_name == "chenxiao"
        assert function_name == "understanding_patch"
        assert '"id": "e1"' in user
        assert "[u1] subject='对玩家的认知'" in user
        assert "linked_episodes" not in user
        assert "<profile>" not in user
        return UnderstandingPatchOutput.model_validate(
            {
                "update": {
                    "u1": {
                        "content": "玩家在压力下会先确认她是否安全。",
                    }
                },
                "add": [
                    {
                        "subject": "互动模式",
                        "content": "玩家会用行动解释误会。",
                    }
                ],
            }
        )

    deleted_ids: list[str] = []
    added_ids: list[str] = []

    async def fake_delete_understanding(uid: str) -> None:
        deleted_ids.append(uid)

    async def fake_add_understanding(understanding: Understanding) -> None:
        added_ids.append(understanding.id)

    monkeypatch.setattr(
        MemoryConsolidationFlow,
        "_run_consolidation_agent",
        fake_run_consolidation_agent,
    )
    monkeypatch.setattr(
        consolidator_module.vector_store,
        "delete_understanding",
        fake_delete_understanding,
    )
    monkeypatch.setattr(
        consolidator_module.vector_store,
        "add_understanding",
        fake_add_understanding,
    )

    await consolidator._patch_understandings(
        agent_name,
        EpisodeMemory(
            id="e1",
            date="10月19日",
            time="10月19日 晚上",
            location="餐厅",
            participants="我、他",
            content="他在她紧张时先确认安全。",
            memory_owner=agent_name,
        ),
    )

    updated = read_understandings(agent_name)
    assert updated["u1"].content == "玩家在压力下会先确认她是否安全。"
    assert updated["u1"].linked_episodes == ["e0", "e1"]
    assert len(updated["u1"].history) == 1
    assert updated["u1"].history[0].episode_id == "e1"
    assert updated["u1"].history[0].date == "10月19日"
    assert updated["u1"].history[0].content == "玩家在压力下会先确认她是否安全。"
    assert updated["new-understanding-id"].content == "玩家会用行动解释误会。"
    assert len(updated["new-understanding-id"].history) == 1
    assert updated["new-understanding-id"].history[0].episode_id == "e1"
    assert updated["new-understanding-id"].history[0].date == "10月19日"
    assert updated["new-understanding-id"].history[0].content == "玩家会用行动解释误会。"
    assert deleted_ids == ["u1"]
    assert added_ids == ["u1", "new-understanding-id"]


@pytest.mark.asyncio
async def test_consolidate_agent_merges_draft_into_memory_and_clears_draft(tmp_path, monkeypatch):
    consolidator = MemoryConsolidationFlow()
    agent_name = "chenxiao"
    path_helper = _make_character_path(tmp_path)

    monkeypatch.setattr(agent_files_module, "character_path", path_helper)
    monkeypatch.setattr(parser_module, "character_path", path_helper)
    monkeypatch.setattr(
        consolidator_module,
        "load_conversation_history",
        lambda **_kw: [
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
