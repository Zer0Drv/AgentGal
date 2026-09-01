"""情绪系统测试：记忆图谱 episode 节点关联「当时心情」。

验证 _episode_node 能把同日期情绪轨迹关联到 episode 节点 meta（emotion / emotion_trace）。
"""

from models import EpisodeMemory
from server import _episode_node


def _episode(**kwargs) -> EpisodeMemory:
    base = {
        "id": "abc-episode",
        "date": "10月2日",
        "title": "新同事入职的第一天",
        "content": "茶水间帮他按咖啡机、递咖啡。",
        "importance": 3,
    }
    base.update(kwargs)
    return EpisodeMemory(**base)


def test_episode_node_attaches_same_day_emotion_trace():
    emotions = [
        {"date": "10月2日", "emotion": "有点开心", "reason": "他被信任"},
        {"date": "10月2日", "emotion": "有点安心", "reason": "感觉被依赖"},
    ]
    node = _episode_node("chenxiao", _episode(), 0, emotions)
    meta = node["meta"]
    # 主导情绪 = 该日最后一条
    assert meta["emotion"] == "有点安心"
    assert meta["emotion_trace"] == ["有点开心", "有点安心"]
    assert meta["emotion_reasons"] == ["他被信任", "感觉被依赖"]


def test_episode_node_ignores_other_days():
    emotions = [
        {"date": "10月3日", "emotion": "生气", "reason": "误会"},
        {"date": "10月2日", "emotion": "开心", "reason": "顺利"},
    ]
    node = _episode_node("chenxiao", _episode(), 0, emotions)
    # 只取 10月2日 的情绪
    assert node["meta"]["emotion"] == "开心"
    assert node["meta"]["emotion_trace"] == ["开心"]


def test_episode_node_filters_empty_emotion_rows():
    emotions = [
        {"date": "10月2日", "emotion": "", "reason": ""},
        {"date": "10月2日", "emotion": "紧张", "reason": "第一次见他"},
    ]
    node = _episode_node("chenxiao", _episode(), 0, emotions)
    assert node["meta"]["emotion"] == "紧张"
    assert node["meta"]["emotion_trace"] == ["紧张"]


def test_episode_node_no_emotion_records():
    node = _episode_node("chenxiao", _episode(), 0, [])
    assert node["meta"]["emotion"] == ""
    assert node["meta"]["emotion_trace"] == []
    assert node["meta"]["emotion_reasons"] == []


def test_episode_node_default_day_emotions_is_none():
    # 不传 day_emotions（向后兼容）：情绪字段为空但不报错
    node = _episode_node("chenxiao", _episode(), 0)
    assert node["meta"]["emotion"] == ""
    assert node["meta"]["emotion_trace"] == []
