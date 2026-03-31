"""测试单条大 user message 的上下文构建逻辑。"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

project_root = Path(__file__).parent.parent
os.chdir(project_root)
sys.path.insert(0, str(project_root))

from engine.prompt_builder import _apply_high_low_watermark, build_history_transcript, build_user_message


@pytest.fixture(autouse=True)
def fake_history_window_state():
    """避免测试写入真实 sidecar，并允许跨调用模拟窗口状态。"""
    state: dict[str, int] = {}

    with patch(
        "engine.prompt_builder.read_sidecar_json",
        side_effect=lambda agent_name, _filename: {"start_raw_index": state.get(agent_name, 0)},
    ), patch(
        "engine.prompt_builder.write_sidecar_json",
        side_effect=lambda agent_name, _filename, data: state.__setitem__(agent_name, data["start_raw_index"]),
    ):
        yield state


# ---------------------------------------------------------------------------
# build_history_transcript
# ---------------------------------------------------------------------------


class TestBuildHistoryTranscript:
    """历史文本构建"""

    def test_player_and_other_roles_have_prefixes(self):
        msgs = [
            {"role": "player", "content": "你好", "visible_to": ["narrator", "lilith"]},
            {"role": "narrator", "content": "场景描述", "visible_to": ["narrator", "lilith"]},
            {"role": "mitsuki", "content": "mitsuki 回复", "visible_to": ["narrator", "lilith"]},
        ]

        result = build_history_transcript("lilith", msgs)

        assert "玩家: 你好" in result
        assert "narrator: 场景描述" in result
        assert "mitsuki: mitsuki 回复" in result

    def test_character_only_sees_visible_messages(self):
        msgs = [
            {"role": "player", "content": "公开消息", "visible_to": ["narrator", "lilith", "mitsuki"]},
            {"role": "mitsuki", "content": "私密回复", "visible_to": ["narrator", "mitsuki"]},
            {"role": "lilith", "content": "lilith回复", "visible_to": ["narrator", "lilith"]},
        ]

        result = build_history_transcript("lilith", msgs)

        assert "公开消息" in result
        assert "lilith: lilith回复" in result
        assert "私密回复" not in result

    def test_narrator_sees_all(self):
        msgs = [
            {"role": "player", "content": "公开", "visible_to": ["narrator", "lilith"]},
            {"role": "mitsuki", "content": "mitsuki", "visible_to": ["narrator", "mitsuki"]},
        ]

        result = build_history_transcript("narrator", msgs)

        assert "玩家: 公开" in result
        assert "mitsuki: mitsuki" in result

    def test_prefix_stable_across_turns(self):
        msgs_turn_n = [
            {"role": "player", "content": "p1", "visible_to": ["narrator", "lilith"]},
            {"role": "narrator", "content": "n1", "visible_to": ["narrator", "lilith"]},
            {"role": "lilith", "content": "l1", "visible_to": ["narrator", "lilith"]},
        ]
        msgs_turn_n1 = msgs_turn_n + [
            {"role": "player", "content": "p2", "visible_to": ["narrator", "lilith"]},
            {"role": "narrator", "content": "n2", "visible_to": ["narrator", "lilith"]},
        ]

        result_n = build_history_transcript("lilith", msgs_turn_n)
        result_n1 = build_history_transcript("lilith", msgs_turn_n1)

        assert result_n1.startswith(result_n + "\n\n")

    def test_truncates_when_exceeds_high(self):
        msgs = [
            {"role": "player", "content": f"消息{i}", "visible_to": ["narrator", "lilith"]}
            for i in range(40)
        ]

        with patch("engine.prompt_builder.HISTORY_HIGH", 30), patch("engine.prompt_builder.HISTORY_LOW", 15):
            result = build_history_transcript("lilith", msgs)

        assert "消息24" not in result
        assert "消息25" in result
        assert "消息39" in result

    def test_true_high_low_window_does_not_slide_every_turn(self):
        msgs_31 = [
            {"role": "player", "content": f"消息{i}", "visible_to": ["narrator", "lilith"]}
            for i in range(31)
        ]
        msgs_32 = msgs_31 + [
            {"role": "player", "content": "消息31", "visible_to": ["narrator", "lilith"]},
        ]
        msgs_47 = [
            {"role": "player", "content": f"消息{i}", "visible_to": ["narrator", "lilith"]}
            for i in range(47)
        ]

        with patch("engine.prompt_builder.HISTORY_HIGH", 30), patch("engine.prompt_builder.HISTORY_LOW", 15):
            result_31 = build_history_transcript("lilith", msgs_31)
            result_32 = build_history_transcript("lilith", msgs_32)
            result_47 = build_history_transcript("lilith", msgs_47)

        assert result_31.startswith("玩家: 消息16")
        assert result_32.startswith("玩家: 消息16")
        assert "消息31" in result_32
        assert result_47.startswith("玩家: 消息32")

    def test_empty_history_returns_empty(self):
        assert build_history_transcript("lilith", []) == ""

    def test_all_filtered_returns_empty(self):
        msgs = [
            {"role": "player", "content": "消息", "visible_to": ["narrator", "mitsuki"]},
        ]

        assert build_history_transcript("lilith", msgs) == ""


class TestHighLowWatermarkHelper:
    """纯逻辑：真正的高低水位缓冲"""

    def test_apply_high_low_watermark_batches_trimming(self):
        start_raw_index, kept = _apply_high_low_watermark("lilith", list(range(31)), 0, 30, 15)
        assert start_raw_index == 16
        assert kept == list(range(16, 31))

        start_raw_index, kept = _apply_high_low_watermark("lilith", list(range(32)), start_raw_index, 30, 15)
        assert start_raw_index == 16
        assert kept == list(range(16, 32))

        start_raw_index, kept = _apply_high_low_watermark("lilith", list(range(47)), start_raw_index, 30, 15)
        assert start_raw_index == 32
        assert kept == list(range(32, 47))


# ---------------------------------------------------------------------------
# build_user_message
# ---------------------------------------------------------------------------


class TestBuildUserMessage:
    """单条大 user message 构建"""

    def test_character_orders_stable_context_before_dynamic_tail(self):
        msgs = [
            {"role": "player", "content": "旧消息", "visible_to": ["narrator", "lilith"]},
            {"role": "narrator", "content": "旧场景", "visible_to": ["narrator", "lilith"]},
        ]

        def fake_read(agent_name: str, filename: str) -> str:
            data = {
                "growth.md": "成长内容",
                "user.md": "玩家画像",
                "status.md": "当前状态",
            }
            return data[filename]

        with patch(
            "engine.prompt_builder.read_agent_file",
            side_effect=fake_read,
        ):
            result = build_user_message(
                "lilith",
                "你好",
                "<relevant_memories>\n记忆内容\n</relevant_memories>",
                raw_messages=msgs,
            )

        assert result.index("<growth>") < result.index("最近对话历史:")
        assert result.index("最近对话历史:") < result.index("<user_profile>")
        assert result.index("最近对话历史:") < result.index("<status>")
        assert result.index("<status>") < result.index("记忆内容")
        assert result.index("记忆内容") < result.index("玩家新消息: 你好")

    def test_character_includes_user_profile_status_memories_input_without_history(self):
        def fake_read(agent_name: str, filename: str) -> str:
            data = {
                "growth.md": "",
                "user.md": "玩家画像",
                "status.md": "当前状态",
            }
            return data[filename]

        with patch(
            "engine.prompt_builder.read_agent_file",
            side_effect=fake_read,
        ):
            result = build_user_message(
                "lilith",
                "你好",
                "<relevant_memories>\n记忆内容\n</relevant_memories>",
                raw_messages=[],
            )

        assert "最近对话历史:" not in result
        assert "<user_profile>" in result
        assert "当前状态" in result
        assert "记忆内容" in result
        assert "玩家新消息: 你好" in result

    def test_narrator_uses_single_big_user_message_without_growth(self):
        msgs = [
            {"role": "player", "content": "旧消息", "visible_to": ["narrator"]},
            {"role": "guyining", "content": "旧回复", "visible_to": ["narrator"]},
        ]

        with patch("engine.prompt_builder.read_agent_file", return_value="故事状态"):
            result = build_user_message("narrator", "新输入", "", raw_messages=msgs)

        assert "<growth>" not in result
        assert "<user_profile>" not in result
        assert "最近对话历史:" in result
        assert result.index("最近对话历史:") < result.index("<status>")
        assert result.index("<status>") < result.index("玩家新消息: 新输入")
