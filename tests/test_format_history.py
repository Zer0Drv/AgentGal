"""测试场景摘要解析"""

from memory.retrieval import _build_retrieval_scene_summary


def test_scene_summary_strips_markdown_and_uses_newlines():
    """测试场景摘要解析使用换行拼接，避免 FTS5 保留符号。"""
    result = _build_retrieval_scene_summary("**地点**：场景摘要")

    assert result == "地点：场景摘要"
    assert "|" not in result
