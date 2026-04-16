from memory.retrieval import _format_retrieved_memory


def test_format_retrieved_memory_includes_date_heading():
    result = _format_retrieved_memory(
        {
            "date": "4月3日",
            "content": "- **时间**：4月3日 08:00\n- **内容**：收到了录取通知书。",
        }
    )

    assert result.startswith("## 4月3日\n")
    assert "收到了录取通知书" in result


def test_format_retrieved_memory_keeps_undated_content_compatible():
    assert _format_retrieved_memory({"content": "旧格式记忆"}) == "旧格式记忆"
