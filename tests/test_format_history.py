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


def test_format_conversation_history_narrator_sees_all():
    """测试 narrator 能看到所有消息"""
    messages = [
        {"role": "player", "content": "玩家消息", "visible_to": ["narrator", "lilith"]},
        {"role": "lilith", "content": "lilith 回复", "visible_to": ["narrator", "lilith"]},
        {"role": "mitsuki", "content": "mitsuki 回复", "visible_to": ["narrator", "mitsuki"]},
    ]
    
    result = app._format_conversation_history(messages, "narrator", limit=10)
    
    # narrator 应该看到所有 3 条消息
    assert "玩家消息" in result
    assert "lilith 回复" in result
    assert "mitsuki 回复" in result
    assert result.count("\n") == 2, "应该有 3 条消息（2 个换行符）"


def test_format_conversation_history_character_sees_only_visible():
    """测试角色只能看到自己可见的消息"""
    messages = [
        {"role": "player", "content": "玩家消息", "visible_to": ["narrator", "lilith"]},
        {"role": "lilith", "content": "lilith 回复", "visible_to": ["narrator", "lilith"]},
        {"role": "mitsuki", "content": "mitsuki 回复", "visible_to": ["narrator", "mitsuki"]},
    ]
    
    result = app._format_conversation_history(messages, "lilith", limit=10)
    
    # lilith 只能看到自己可见的消息
    assert "玩家消息" in result
    assert "lilith 回复" in result
    assert "mitsuki 回复" not in result
    assert result.count("\n") == 1, "应该有 2 条消息（1 个换行符）"


def test_format_conversation_history_limit():
    """测试 limit 参数正确限制返回数量"""
    messages = [
        {"role": "player", "content": f"消息 {i}", "visible_to": ["narrator"]}
        for i in range(5)
    ]
    
    result = app._format_conversation_history(messages, "narrator", limit=2)
    
    # 应该只返回最后 2 条
    assert "消息 3" in result
    assert "消息 4" in result
    assert "消息 0" not in result
    assert result.count("\n") == 1, "应该有 2 条消息（1 个换行符）"


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

