"""检索 query 组装测试。"""

from memory.retrieval import _build_retrieval_scene_summary


def test_scene_summary_extracts_time_location_present():
    scene = _build_retrieval_scene_summary(
        "**时间**：10月3日 星期二 18:27\n**地点**：梧桐街咖啡馆内\n**在场**：\n- 玩家：顾以宁对面的座位\n- 顾以宁：靠窗座位\n窗外梧桐叶在晚风里轻晃。"
    )

    assert scene == (
        "时间：10月3日 星期二 18:27\n"
        "地点：梧桐街咖啡馆内\n"
        "在场：\n"
        "玩家：顾以宁对面的座位\n"
        "顾以宁：靠窗座位"
    )


def test_scene_summary_skips_absent_present_lines():
    scene = _build_retrieval_scene_summary(
        "**地点**：汤包店靠窗小桌\n- 玩家：自己家中（不在场）\n- 陈晓：沙发上，抱着靠枕"
    )

    assert scene == "地点：汤包店靠窗小桌"
