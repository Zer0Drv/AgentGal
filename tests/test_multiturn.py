"""测试单条大 user message 的上下文构建逻辑。"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

project_root = Path(__file__).parent.parent
os.chdir(project_root)
sys.path.insert(0, str(project_root))

import engine.memory_query_builder as query_builder_module
from engine.memory_query_builder import build_retrieval_queries
from engine.prompt_builder import (
    _apply_high_low_watermark,
    build_history_transcript,
    build_user_message,
)


@pytest.fixture(autouse=True)
def fake_history_window_state():
    """避免测试写入真实 sidecar，并允许跨调用模拟窗口状态。"""
    state: dict[str, int] = {}

    with patch(
        "engine.prompt_builder.read_sidecar_json",
        side_effect=lambda agent_name, _filename: {"start_turn": state.get(agent_name, 0)},
    ), patch(
        "engine.prompt_builder.write_sidecar_json",
        side_effect=lambda agent_name, _filename, data: state.__setitem__(agent_name, data["start_turn"]),
    ):
        yield state


# ---------------------------------------------------------------------------
# build_history_transcript
# ---------------------------------------------------------------------------


class TestBuildHistoryTranscript:
    """历史文本构建"""

    def test_returns_all_visible_messages_in_window(self):
        msgs = [
            {"role": "player", "content": "你好", "visible_to": ["narrator", "lilith"]},
            {"role": "narrator", "content": "旧场景描述", "visible_to": ["narrator", "lilith"]},
            {"role": "mitsuki", "content": "mitsuki 回复", "visible_to": ["narrator", "lilith"]},
            {"role": "narrator", "content": "新场景描述", "visible_to": ["narrator", "lilith"]},
        ]

        result, _ = build_history_transcript("lilith", msgs)

        assert result == "玩家: 你好\n\n旁白: 旧场景描述\n\nmitsuki: mitsuki 回复\n\n旁白: 新场景描述"

    def test_character_only_sees_visible_messages(self):
        msgs = [
            {"role": "narrator", "content": "公开场景", "visible_to": ["narrator", "lilith", "mitsuki"]},
            {"role": "narrator", "content": "mitsuki 私密场景", "visible_to": ["narrator", "mitsuki"]},
            {"role": "lilith", "content": "lilith回复", "visible_to": ["narrator", "lilith"]},
        ]

        result, _ = build_history_transcript("lilith", msgs)

        assert result == "旁白: 公开场景\n\nlilith: lilith回复"
        assert "mitsuki 私密场景" not in result

    def test_uses_last_narrator_visible_to_agent_not_global_latest(self):
        msgs = [
            {"role": "player", "content": "公开消息", "visible_to": ["narrator", "lilith", "mitsuki"]},
            {"role": "narrator", "content": "lilith 可见场景", "visible_to": ["narrator", "lilith"]},
            {"role": "mitsuki", "content": "私密回复", "visible_to": ["narrator", "mitsuki"]},
            {"role": "narrator", "content": "mitsuki 私密场景", "visible_to": ["narrator", "mitsuki"]},
        ]

        result, _ = build_history_transcript("lilith", msgs)

        assert result == "玩家: 公开消息\n\n旁白: lilith 可见场景"
        assert "mitsuki 私密场景" not in result
        assert "私密回复" not in result

    def test_narrator_sees_all_messages_in_window(self):
        msgs = [
            {"role": "player", "content": "公开", "visible_to": ["narrator", "lilith"]},
            {"role": "narrator", "content": "旧场景", "visible_to": ["narrator", "lilith"]},
            {"role": "mitsuki", "content": "mitsuki", "visible_to": ["narrator", "mitsuki"]},
            {"role": "narrator", "content": "最新场景", "visible_to": ["narrator", "mitsuki"]},
        ]

        result, _ = build_history_transcript("narrator", msgs)

        assert result == "玩家: 公开\n\n旁白: 旧场景\n\nmitsuki: mitsuki\n\n旁白: 最新场景"

    def test_prefix_stable_across_turns_while_old_narrator_is_retained(self):
        msgs_turn_n = [
            {"role": "player", "content": "p1", "visible_to": ["narrator", "lilith"]},
            {"role": "narrator", "content": "n1", "visible_to": ["narrator", "lilith"]},
            {"role": "lilith", "content": "l1", "visible_to": ["narrator", "lilith"]},
        ]
        msgs_turn_n1 = msgs_turn_n + [
            {"role": "player", "content": "p2", "visible_to": ["narrator", "lilith"]},
            {"role": "narrator", "content": "n2", "visible_to": ["narrator", "lilith"]},
        ]

        result_n, _ = build_history_transcript("lilith", msgs_turn_n)
        result_n1, _ = build_history_transcript("lilith", msgs_turn_n1)

        assert result_n == "玩家: p1\n\n旁白: n1\n\nlilith: l1"
        assert result_n1 == "玩家: p1\n\n旁白: n1\n\nlilith: l1\n\n玩家: p2\n\n旁白: n2"

    def test_truncates_when_exceeds_high(self):
        msgs = [
            {"role": "narrator", "content": f"消息{i}", "turn": i, "visible_to": ["narrator", "lilith"]}
            for i in range(40)
        ]

        with patch("engine.prompt_builder.HISTORY_HIGH", 30), patch("engine.prompt_builder.HISTORY_LOW", 15):
            result, was_truncated = build_history_transcript("lilith", msgs)

        assert result == "\n\n".join(f"旁白: 消息{i}" for i in range(25, 40))
        assert was_truncated is True

    def test_true_high_low_window_does_not_slide_every_turn(self):
        msgs_31 = [
            {"role": "narrator", "content": f"消息{i}", "turn": i, "visible_to": ["narrator", "lilith"]}
            for i in range(31)
        ]
        msgs_32 = msgs_31 + [
            {"role": "narrator", "content": "消息31", "turn": 31, "visible_to": ["narrator", "lilith"]},
        ]
        msgs_47 = [
            {"role": "narrator", "content": f"消息{i}", "turn": i, "visible_to": ["narrator", "lilith"]}
            for i in range(47)
        ]

        with patch("engine.prompt_builder.HISTORY_HIGH", 30), patch("engine.prompt_builder.HISTORY_LOW", 15):
            result_31, truncated_31 = build_history_transcript("lilith", msgs_31)
            result_32, truncated_32 = build_history_transcript("lilith", msgs_32)
            result_47, truncated_47 = build_history_transcript("lilith", msgs_47)

        assert result_31 == "\n\n".join(f"旁白: 消息{i}" for i in range(16, 31))
        assert truncated_31 is True
        assert result_32 == "\n\n".join(f"旁白: 消息{i}" for i in range(16, 32))
        assert truncated_32 is False
        assert result_47 == "\n\n".join(f"旁白: 消息{i}" for i in range(32, 47))
        assert truncated_47 is True

    def test_empty_history_returns_empty(self):
        result, _ = build_history_transcript("lilith", [])
        assert result == ""

    def test_visible_non_narrator_messages_are_kept(self):
        msgs = [
            {"role": "player", "content": "消息", "visible_to": ["narrator", "lilith"]},
            {"role": "lilith", "content": "回复", "visible_to": ["narrator", "lilith"]},
        ]

        result, _ = build_history_transcript("lilith", msgs)
        assert result == "玩家: 消息\n\nlilith: 回复"

    def test_all_filtered_returns_empty(self):
        msgs = [
            {"role": "narrator", "content": "消息", "visible_to": ["narrator", "mitsuki"]},
        ]

        result, _ = build_history_transcript("lilith", msgs)
        assert result == ""


class TestBuildMemoryQueryBuilder:
    """角色 RAG query 构建"""

    @staticmethod
    def _raw_messages() -> list[dict]:
        return [
            {
                "role": "player",
                "content": "私密词",
                "visible_to": ["shizuka", "narrator"],
                "turn": 1,
            },
            {
                "role": "player",
                "content": "## 北原悠\n才不是……你说「我也有夏帆了，我们珍惜身边人」",
                "visible_to": ["mitsuki", "narrator"],
                "turn": 2,
            },
            {
                "role": "narrator",
                "date": "4月16日 星期日",
                "time": "17:09",
                "location": "一之濑美月家客厅",
                "present_characters": {
                    "北原悠": "沙发上，抬眼看着她",
                    "一之濑美月": "跪坐在沙发上，双手搭在他肩头",
                },
                "scene_description": "美月听到他提起旧话后微微一怔。",
                "new_characters": [],
                "targets": ["mitsuki"],
                "visible_to": ["mitsuki", "narrator"],
                "turn": 3,
            },
        ]

    def test_search_query_uses_scene_and_recent_visible_dialogue(self, monkeypatch):
        monkeypatch.setattr(
            query_builder_module,
            "get_narrative_focus",
            lambda _agent_name: "美月提起车站和家门口的拒绝",
        )

        query = build_retrieval_queries(
            "mitsuki",
            "我不想再被拒绝",
            self._raw_messages(),
        ).episode

        assert "当前场景" in query
        assert "一之濑美月家客厅" in query
        assert "最近对话" in query
        assert "珍惜身边人" in query
        assert "家门口的拒绝" in query
        assert "私密词" not in query

    def test_memory_bm25_query_keeps_high_signal_anchors(self, monkeypatch):
        monkeypatch.setattr(
            query_builder_module,
            "get_narrative_focus",
            lambda _agent_name: "美月想收回车站和家门口那两次拒绝",
        )

        query = build_retrieval_queries(
            "mitsuki",
            "可是我还不想这么快做决定",
            self._raw_messages(),
        ).episode_bm25

        assert "我也有夏帆了，我们珍惜身边人" in query
        assert "北原悠" in query
        assert "一之濑美月" in query
        assert "一之濑美月家客厅" in query
        assert "拒绝" in query
        assert "私密词" not in query
        assert "玩家" not in query

    def test_queries_ignore_ambient_present_character_names(self, monkeypatch):
        monkeypatch.setattr(
            query_builder_module,
            "get_narrative_focus",
            lambda _agent_name: "一之濑美月低声和北原悠约定放学后见面",
        )
        raw_messages = [
            {
                "targets": ["mitsuki"],
                "date": "4月19日 星期四",
                "time": "10:08",
                "location": "城川私立高中高二B班教室",
                "present_characters": {
                    "北原悠": "靠窗座位上",
                    "一之濑美月": "前排座位",
                    "川上夏帆": "后排和女生轻声聊天",
                    "森野静": "靠墙座位翻书",
                },
                "scene_description": "喧闹的课间还在持续，美月手里的铅笔在笔记本上轻轻划了一道。",
                "new_characters": [],
                "role": "narrator",
                "visible_to": ["mitsuki", "narrator"],
                "turn": 1,
            },
            {
                "role": "player",
                "content": "## 北原悠\n不可以想逛街吗？",
                "visible_to": ["mitsuki", "narrator"],
                "turn": 1,
            },
        ]

        queries = build_retrieval_queries("mitsuki", "不可以想逛街吗？", raw_messages)

        assert "城川私立高中高二B班教室" in queries.episode
        assert "喧闹的课间" in queries.episode
        assert "川上夏帆" not in queries.episode
        assert "川上夏帆" not in queries.episode_bm25
        assert "川上夏帆" not in queries.understanding
        assert "川上夏帆" not in queries.understanding_bm25

    def test_episode_query_uses_player_message_from_history_once(self, monkeypatch):
        monkeypatch.setattr(
            query_builder_module,
            "get_narrative_focus",
            lambda _agent_name: "美月拉着北原悠往楼梯口走去",
        )
        raw_messages = [
            {
                "targets": ["mitsuki"],
                "date": "4月19日 星期四",
                "time": "10:16",
                "location": "教学楼走廊，楼梯口旁",
                "scene_description": "美月的手微微一紧，随即稍稍松开了一点力道。",
                "new_characters": [],
                "role": "narrator",
                "visible_to": ["mitsuki", "narrator"],
                "turn": 10,
            },
            {
                "role": "mitsuki",
                "content": "## 一之濑美月\n（轻轻扯住他的袖口）等她走过去我们再走。",
                "visible_to": ["mitsuki", "narrator"],
                "turn": 10,
            },
            {
                "role": "player",
                "content": "## 北原悠\n这样拉着我，等下碰到森野了怎么办",
                "visible_to": ["mitsuki", "narrator"],
                "turn": 10,
            },
        ]

        queries = build_retrieval_queries(
            "mitsuki",
            "这样拉着我，等下碰到森野了怎么办",
            raw_messages,
        )

        assert queries.episode.count("这样拉着我，等下碰到森野了怎么办") == 1
        assert queries.episode_bm25.count("这样拉着我，等下碰到森野了怎么办") == 1
        assert queries.understanding.count("这样拉着我，等下碰到森野了怎么办") == 1
        assert queries.understanding_bm25.count("这样拉着我，等下碰到森野了怎么办") == 1
        assert "玩家新消息" not in queries.episode
        assert "当前互动" not in queries.understanding
        assert "当前互动" not in queries.understanding_bm25
        assert "近期可见对话" not in queries.understanding_bm25
        assert "玩家: ## 北原悠" in queries.episode

    def test_understanding_query_uses_relation_context_not_focus_only(self, monkeypatch):
        monkeypatch.setattr(
            query_builder_module,
            "get_narrative_focus",
            lambda _agent_name: "美月想收回车站和家门口那两次拒绝",
        )

        queries = build_retrieval_queries(
            "mitsuki",
            "可是我还不想这么快做决定",
            self._raw_messages(),
        )

        assert "关系/行为焦点" in queries.understanding
        assert "近期可见对话" in queries.understanding
        assert "珍惜身边人" in queries.understanding
        assert "关系/行为焦点" not in queries.understanding_bm25
        assert "近期可见对话" not in queries.understanding_bm25
        assert "珍惜身边人" in queries.understanding_bm25
        assert "私密词" not in queries.understanding
        assert "私密词" not in queries.understanding_bm25

    def test_queries_keep_dialogue_when_no_scene_message(self, monkeypatch):
        monkeypatch.setattr(
            query_builder_module,
            "get_narrative_focus",
            lambda _agent_name: "",
        )
        raw_messages = [
            {
                "role": "player",
                "content": "## 北原悠\n我害怕再次被拒绝",
                "visible_to": ["mitsuki", "narrator"],
                "turn": 1,
            },
        ]

        queries = build_retrieval_queries("mitsuki", "我还需要时间", raw_messages)

        assert "我害怕再次被拒绝" in queries.episode
        assert "我害怕再次被拒绝" in queries.episode_bm25
        assert "我害怕再次被拒绝" in queries.understanding
        assert "我害怕再次被拒绝" in queries.understanding_bm25


class TestHighLowWatermarkHelper:
    """纯逻辑：真正的高低水位缓冲（基于 turn 锚定）"""

    @staticmethod
    def _msgs(n: int) -> list[dict]:
        return [{"role": "narrator", "content": f"m{i}", "turn": i} for i in range(n)]

    def test_apply_high_low_watermark_batches_trimming(self):
        anchor, kept, was_truncated = _apply_high_low_watermark(self._msgs(31), 0, 30, 15)
        assert anchor == 16
        assert [m["turn"] for m in kept] == list(range(16, 31))
        assert was_truncated is True

        anchor, kept, was_truncated = _apply_high_low_watermark(self._msgs(32), anchor, 30, 15)
        assert anchor == 16
        assert [m["turn"] for m in kept] == list(range(16, 32))
        assert was_truncated is False

        anchor, kept, was_truncated = _apply_high_low_watermark(self._msgs(47), anchor, 30, 15)
        assert anchor == 32
        assert [m["turn"] for m in kept] == list(range(32, 47))
        assert was_truncated is True


# ---------------------------------------------------------------------------
# build_user_message
# ---------------------------------------------------------------------------


class TestBuildUserMessage:
    """单条大 user message 构建"""

    def test_character_orders_profile_before_history_and_dynamic_tail(self):
        msgs = [
            {"role": "player", "content": "旧消息", "visible_to": ["narrator", "lilith"]},
            {"role": "narrator", "content": "旧场景", "visible_to": ["narrator", "lilith"]},
        ]

        def fake_read(agent_name: str, filename: str) -> str:
            data = {
                "status.md": "当前状态",
            }
            return data[filename]

        with patch(
            "engine.prompt_builder.read_agent_file",
            side_effect=fake_read,
        ):
            result, _ = build_user_message(
                "lilith",
                "你好",
                "<relevant_memories>\n记忆内容\n</relevant_memories>",
                raw_messages=msgs,
            )

        assert result.index("最近对话历史:") < result.index("<status>")
        assert result.index("<status>") < result.index("记忆内容")
        assert result.index("记忆内容") < result.index("玩家新消息：你好")

    def test_character_includes_user_profile_status_memories_input_without_history(self):
        def fake_read(agent_name: str, filename: str) -> str:
            data = {
                "status.md": "当前状态",
            }
            return data[filename]

        with patch(
            "engine.prompt_builder.read_agent_file",
            side_effect=fake_read,
        ):
            result, _ = build_user_message(
                "lilith",
                "你好",
                "<relevant_memories>\n记忆内容\n</relevant_memories>",
                raw_messages=[],
            )

        assert "最近对话历史:" not in result
        assert "当前状态" in result
        assert "记忆内容" in result
        assert "玩家新消息：你好" in result

    def test_narrator_uses_single_big_user_message_with_status_and_input(self):
        msgs = [
            {"role": "player", "content": "旧消息", "visible_to": ["narrator"]},
            {"role": "narrator", "content": "旧场景", "visible_to": ["narrator"]},
            {"role": "guyining", "content": "旧回复", "visible_to": ["narrator"]},
        ]

        with patch("engine.prompt_builder.read_agent_file", return_value="故事状态"):
            result, _ = build_user_message("narrator", "新输入", "", raw_messages=msgs)

        assert "最近对话历史:" in result
        assert "玩家: 旧消息" in result
        assert "旁白: 旧场景" in result
        assert "guyining: 旧回复" in result
        assert result.index("最近对话历史:") < result.index("<status>")
        assert result.index("<status>") < result.index("玩家新消息：新输入")
