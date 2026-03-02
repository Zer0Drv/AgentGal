"""测试对话历史格式化功能

测试 _format_conversation_history 能否正确过滤和格式化消息
"""

import sys
from pathlib import Path

import pytest

# 设置项目根目录
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 导入 app.py 中的 _format_conversation_history
import importlib.util
spec = importlib.util.spec_from_file_location("app", project_root / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def test_format_conversation_history_filters_old_narrator():
    """测试只保留最新的 narrator 发言，过滤掉其他 narrator 发言"""
    messages = [
        {"role": "narrator", "content": "旧场景描述", "visible_to": ["narrator", "lilith"]},
        {"role": "player", "content": "玩家消息", "visible_to": ["narrator", "lilith"]},
        {"role": "lilith", "content": "lilith 回复", "visible_to": ["narrator", "lilith"]},
        {"role": "narrator", "content": "新场景描述", "visible_to": ["narrator", "lilith"]},
    ]

    result = app._format_conversation_history(messages, "lilith", limit=10)

    # 应该只保留最新的 narrator 发言
    assert "新场景描述" in result
    assert "旧场景描述" not in result
    assert "玩家消息" in result
    assert "lilith 回复" in result
    # 应该有 3 条消息（玩家 + lilith + 最新narrator）
    assert result.count("\n") == 2, f"应该有 3 条消息，实际: {result}"


def test_format_conversation_history_character_sees_only_visible():
    """测试角色只能看到自己可见的消息，且过滤旧 narrator"""
    messages = [
        {"role": "narrator", "content": "旧场景", "visible_to": ["narrator", "lilith"]},
        {"role": "player", "content": "玩家消息", "visible_to": ["narrator", "lilith"]},
        {"role": "lilith", "content": "lilith 回复", "visible_to": ["narrator", "lilith"]},
        {"role": "mitsuki", "content": "mitsuki 回复", "visible_to": ["narrator", "mitsuki"]},
        {"role": "narrator", "content": "新场景", "visible_to": ["narrator", "lilith"]},
    ]

    result = app._format_conversation_history(messages, "lilith", limit=10)

    # lilith 只能看到自己可见的消息，且只有最新的 narrator
    assert "玩家消息" in result
    assert "lilith 回复" in result
    assert "新场景" in result
    assert "旧场景" not in result
    assert "mitsuki 回复" not in result
    assert result.count("\n") == 2, "应该有 3 条消息（2 个换行符）"


def test_format_conversation_history_limit_with_narrator():
    """测试 limit 参数，确保返回指定数量的有效消息"""
    # 创建 20 条消息：10 条旧 narrator + 10 条玩家消息
    messages = []
    for i in range(10):
        messages.append({"role": "narrator", "content": f"旧场景 {i}", "visible_to": ["narrator"]})
        messages.append({"role": "player", "content": f"消息 {i}", "visible_to": ["narrator"]})
    # 最后加一条新 narrator
    messages.append({"role": "narrator", "content": "新场景", "visible_to": ["narrator"]})

    result = app._format_conversation_history(messages, "narrator", limit=5)

    # 应该返回 5 条有效消息（不包括旧narrator）
    # 由于加载 limit*3=15 条，会包含最后 5 个玩家消息 + 最新narrator
    lines = result.split("\n")
    assert len(lines) == 5, f"应该有 5 条消息，实际: {len(lines)}, 内容: {result}"
    assert "新场景" in result
    assert "旧场景" not in result


def test_format_conversation_history_empty():
    """测试空消息列表返回空字符串"""
    result = app._format_conversation_history([], "narrator", limit=10)
    assert result == "", "空消息列表应该返回空字符串"


def test_format_conversation_history_formatting():
    """测试消息格式化正确"""
    messages = [
        {"role": "player", "content": "你好", "visible_to": ["narrator"]},
        {"role": "narrator", "content": "你好，欢迎", "visible_to": ["narrator"]},
    ]

    result = app._format_conversation_history(messages, "narrator", limit=10)

    # 检查格式
    assert "玩家: 你好" in result
    assert "narrator: 你好，欢迎" in result


def test_format_conversation_history_no_narrator():
    """测试没有 narrator 发言的情况"""
    messages = [
        {"role": "player", "content": "玩家消息 1", "visible_to": ["narrator", "lilith"]},
        {"role": "lilith", "content": "lilith 回复", "visible_to": ["narrator", "lilith"]},
        {"role": "player", "content": "玩家消息 2", "visible_to": ["narrator", "lilith"]},
    ]

    result = app._format_conversation_history(messages, "lilith", limit=10)

    # 没有 narrator 发言，应该保留所有消息
    assert "玩家消息 1" in result
    assert "lilith 回复" in result
    assert "玩家消息 2" in result
    assert result.count("\n") == 2, "应该有 3 条消息（2 个换行符）"

