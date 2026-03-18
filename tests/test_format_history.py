"""测试对话历史格式化功能

测试 format_conversation_history 能否正确过滤和格式化消息
"""

from engine.agent_manager import format_conversation_history
from memory.retrieval import build_search_query


def test_format_conversation_history_filters_old_narrator():
    """测试只保留最新的 narrator 发言，过滤掉其他 narrator 发言"""
    messages = [
        {"role": "narrator", "content": "旧场景描述", "visible_to": ["narrator", "lilith"]},
        {"role": "player", "content": "玩家消息", "visible_to": ["narrator", "lilith"]},
        {"role": "lilith", "content": "lilith 回复", "visible_to": ["narrator", "lilith"]},
        {"role": "narrator", "content": "新场景描述", "visible_to": ["narrator", "lilith"]},
    ]

    result = format_conversation_history(messages, "lilith", limit=10)

    # 应该只保留最新的 narrator 发言
    assert "新场景描述" in result
    assert "旧场景描述" not in result
    assert "玩家消息" in result
    assert "lilith 回复" in result
    # 应该有 3 条消息（玩家 + lilith + 最新narrator），用 \n\n 分隔
    assert result.count("\n\n") == 2, f"应该有 3 条消息，实际: {result}"


def test_format_conversation_history_character_sees_only_visible():
    """测试角色只能看到自己可见的消息，且过滤旧 narrator"""
    messages = [
        {"role": "narrator", "content": "旧场景", "visible_to": ["narrator", "lilith"]},
        {"role": "player", "content": "玩家消息", "visible_to": ["narrator", "lilith"]},
        {"role": "lilith", "content": "lilith 回复", "visible_to": ["narrator", "lilith"]},
        {"role": "mitsuki", "content": "mitsuki 回复", "visible_to": ["narrator", "mitsuki"]},
        {"role": "narrator", "content": "新场景", "visible_to": ["narrator", "lilith"]},
    ]

    result = format_conversation_history(messages, "lilith", limit=10)

    # lilith 只能看到自己可见的消息，且只有最新的 narrator
    assert "玩家消息" in result
    assert "lilith 回复" in result
    assert "新场景" in result
    assert "旧场景" not in result
    assert "mitsuki 回复" not in result
    assert result.count("\n\n") == 2, "应该有 3 条消息（2 个双换行分隔符）"


def test_format_conversation_history_limit_with_narrator():
    """测试 limit 参数，确保返回指定数量的有效消息（考虑visible_to过滤）"""
    # 创建 50 条消息：
    # - 25 条旧 narrator
    # - 25 条玩家消息（对lilith可见）
    # - 25 条mitsuki消息（对lilith不可见）
    messages = []
    for i in range(25):
        messages.append({"role": "narrator", "content": f"旧场景 {i}", "visible_to": ["narrator", "lilith"]})
        messages.append({"role": "player", "content": f"消息 {i}", "visible_to": ["narrator", "lilith"]})
        messages.append({"role": "mitsuki", "content": f"mitsuki {i}", "visible_to": ["narrator", "mitsuki"]})
    # 最后加一条新 narrator
    messages.append({"role": "narrator", "content": "新场景", "visible_to": ["narrator", "lilith"]})

    result = format_conversation_history(messages, "lilith", limit=10)

    # lilith 应该看到 10 条有效消息
    # 由于加载 limit*5=50 条，过滤后只有lilith可见的消息
    # 然后过滤旧narrator，最后取10条
    parts = result.split("\n\n")
    assert len(parts) == 10, f"应该有 10 条消息，实际: {len(parts)}, 内容: {result}"
    assert "新场景" in result
    assert "旧场景" not in result
    assert "mitsuki" not in result  # lilith 看不到 mitsuki 的消息


def test_format_conversation_history_empty():
    """测试空消息列表返回空字符串"""
    result = format_conversation_history([], "narrator", limit=10)
    assert result == "", "空消息列表应该返回空字符串"


def test_format_conversation_history_formatting():
    """测试消息格式化正确"""
    messages = [
        {"role": "player", "content": "你好", "visible_to": ["narrator"]},
        {"role": "narrator", "content": "你好，欢迎", "visible_to": ["narrator"]},
    ]

    result = format_conversation_history(messages, "narrator", limit=10)

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

    result = format_conversation_history(messages, "lilith", limit=10)

    # 没有 narrator 发言，应该保留所有消息
    assert "玩家消息 1" in result
    assert "lilith 回复" in result
    assert "玩家消息 2" in result
    assert result.count("\n\n") == 2, "应该有 3 条消息（2 个双换行分隔符）"


def test_build_search_query_uses_newlines():
    """测试记忆检索 query 使用换行拼接，避免 FTS5 保留符号。"""
    result = build_search_query("lilith", "玩家原话", "**地点**：场景摘要")

    assert result == "玩家原话\n地点：场景摘要"
    assert "|" not in result
