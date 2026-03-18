"""检索 query 组装测试。"""

from memory.retrieval import build_search_queries


def test_character_search_queries_keep_raw_input_and_scene():
    vector_query, bm25_query = build_search_queries(
        "guyining",
        "（深吸了一口气，认真）顾总，我怕我误会，所以直接问了，我们现在是在约会吗？",
        "**时间**：10月3日 星期二 18:27\n**地点**：梧桐街咖啡馆内\n**在场**：\n- 玩家：顾以宁对面的座位\n- 顾以宁：靠窗座位\n窗外梧桐叶在晚风里轻晃。",
        "这段历史现在不参与 query 改写",
    )

    assert vector_query == (
        "（深吸了一口气，认真）顾总，我怕我误会，所以直接问了，我们现在是在约会吗？\n"
        "时间：10月3日 星期二 18:27\n"
        "地点：梧桐街咖啡馆内\n"
        "在场：\n"
        "玩家：顾以宁对面的座位\n"
        "顾以宁：靠窗座位"
    )
    assert bm25_query == vector_query


def test_character_search_queries_fall_back_to_vector_query_when_input_empty():
    vector_query, bm25_query = build_search_queries(
        "chenxiao",
        "",
        "**地点**：汤包店靠窗小桌\n- 玩家：自己家中（不在场）\n- 陈晓：沙发上，抱着靠枕",
    )

    assert vector_query == "地点：汤包店靠窗小桌"
    assert bm25_query == vector_query
