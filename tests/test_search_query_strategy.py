"""查询改写策略测试。"""

from textwrap import dedent

from memory.retrieval import build_search_queries


def test_search_queries_for_direct_relationship_question():
    """长句玩家原话 + markdown 场景，应被改写成紧凑的语义摘要和关键词。"""
    scene_summary = dedent(
        """
        **时间**：10月3日 星期二 18:27
        **地点**：梧桐街咖啡馆内
        **在场**：
        - 玩家：顾以宁对面的座位
        - 顾以宁：靠窗座位
        窗外梧桐叶在晚风里轻晃。
        """
    ).strip()
    history = dedent(
        """
        guyining: ## 顾以宁
        （端起自己的美式抿了一口，视线跟着他看向窗外）
        嗯，安静。
        （放下杯子，声音轻了些）所以带你来。
        """
    ).strip()

    vector_query, bm25_query = build_search_queries(
        "guyining",
        "（深吸了一口气，认真）顾总，我怕我误会，所以直接问了，我们现在是在约会吗？",
        scene_summary,
        history,
    )

    assert vector_query == (
        "顾以宁和玩家在梧桐街咖啡馆内独处。"
        "玩家说：\"顾总，我怕我误会，所以直接问了，我们现在是在约会吗？\""
        "当前话题是两人是否在约会。"
    )
    assert bm25_query == "顾以宁 玩家 梧桐街 咖啡馆 独处 约会 误会 确认"


def test_search_queries_add_topic_anchor_for_short_followup_line():
    """很短、很虚的承接句，应从最近对话补出主题锚点。"""
    scene_summary = dedent(
        """
        **时间**：10月3日 星期二 19:10
        **地点**：梧桐街路口，顾以宁的车旁
        **在场**：
        - 玩家：副驾驶座旁，还没上车
        - 顾以宁：驾驶座旁，手搭在车门把手上
        路灯的光线从梧桐叶的缝隙里漏下来。
        """
    ).strip()
    history = dedent(
        """
        玩家: （跟着她的速度，也放得很慢）你这个走路速度（笑）不是想送我回去，倒有点像……
        （红着脸别过头）跟我回去

        guyining: ## 顾以宁
        （看了他两秒，唇角很轻地动了一下）
        你要是这么理解，也不算错。
        """
    ).strip()

    vector_query, bm25_query = build_search_queries(
        "guyining",
        "（脸颊有些发烫）我…这个，呃，如果我说是后者，会不会太快？",
        scene_summary,
        history,
    )

    assert vector_query == (
        "顾以宁和玩家在梧桐街路口、车旁独处。"
        "玩家说：\"如果我说是后者，会不会太快？\""
        "当前话题是送回去前的暧昧试探。"
    )
    assert bm25_query == "顾以宁 玩家 梧桐街 路口 车旁 送回去 暧昧 试探 太快"
    assert "*" not in bm25_query
    assert ":" not in bm25_query
