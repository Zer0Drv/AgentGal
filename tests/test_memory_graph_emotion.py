"""情绪系统测试：记忆图谱 episode 节点关联「当时心情」（最近邻归属）。

_assign_emotions_to_episodes 把情绪按「时段中心最近邻」归属到各 episode；
_episode_node 展示归属好的情绪。
"""

from models import EpisodeMemory
from server import _assign_emotions_to_episodes, _episode_node


def _episode(eid: str, date: str = "10月2日", time: str = "") -> EpisodeMemory:
    return EpisodeMemory(
        id=eid,
        date=date,
        time=time,
        title=f"记忆{eid}",
        content="内容",
        importance=3,
    )


def _emo(emotion: str, date: str = "10月2日", time: str = "", reason: str = "") -> dict:
    return {"date": date, "time": time, "emotion": emotion, "reason": reason}


# ---- 最近邻归属 _assign_emotions_to_episodes ----


def test_assign_emotion_to_nearest_episode_center():
    # episode1 09:20-09:27（中心 09:23.5），episode2 09:30-09:35（中心 09:32.5）
    eps = [
        _episode("e1", time="10月2日 09:20-09:27"),
        _episode("e2", time="10月2日 09:30-09:35"),
    ]
    emotions = [
        _emo("有点开心", time="星期一 09:30"),
        _emo("有点紧张", time="星期一 09:38"),
    ]
    assigned = _assign_emotions_to_episodes(eps, emotions)
    # e2 精准拿到 09:30/09:38；e1 时段无情绪 → 回退到时间最近一条（09:30）
    assert [e["emotion"] for e in assigned["e2"]] == ["有点开心", "有点紧张"]
    assert [e["emotion"] for e in assigned["e1"]] == ["有点开心"]


def test_assign_emotion_falls_back_to_nearest_across_days():
    # episode 当天无任何情绪 → 回退到全局时间最近的（跨日期）
    eps = [_episode("e1", date="10月5日", time="10月5日 09:00-09:10")]
    emotions = [_emo("平静", date="10月2日", time="09:00")]
    assigned = _assign_emotions_to_episodes(eps, emotions)
    assert [e["emotion"] for e in assigned["e1"]] == ["平静"]


def test_assign_emotion_split_across_episodes():
    eps = [
        _episode("e1", time="10月2日 08:00-08:30"),
        _episode("e2", time="10月2日 12:00-12:30"),
    ]
    emotions = [_emo("平静", time="08:10"), _emo("开心", time="12:10")]
    assigned = _assign_emotions_to_episodes(eps, emotions)
    assert [e["emotion"] for e in assigned["e1"]] == ["平静"]
    assert [e["emotion"] for e in assigned["e2"]] == ["开心"]


def test_assign_emotion_fallback_to_first_when_no_clock():
    # episode 无时刻、情绪带时刻 → 无法算中心，保底归当天第一个 episode
    eps = [_episode("e1", time="10月2日"), _episode("e2", time="10月2日 12:00")]
    emotions = [_emo("开心", time="09:00")]
    assigned = _assign_emotions_to_episodes(eps, emotions)
    # e1 无时刻，情绪 09:00 的最近邻只在有时刻的 e2 里算 → 归 e2
    assert [e["emotion"] for e in assigned["e2"]] == ["开心"]


def test_assign_emotion_ignores_other_date():
    eps = [_episode("e1", date="10月2日")]
    emotions = [_emo("生气", date="10月3日", time="09:00")]
    assigned = _assign_emotions_to_episodes(eps, emotions)
    assert assigned["e1"] == []


# ---- _episode_node 展示 ----


def test_episode_node_attaches_assigned_emotion():
    emotions = [
        _emo("有点开心", time="09:30", reason="被信任"),
        _emo("有点安心", time="09:35", reason="感觉被依赖"),
    ]
    node = _episode_node("chenxiao", _episode("e1", time="10月2日 09:30-09:35"), 0, emotions)
    meta = node["meta"]
    assert meta["emotion"] == "有点安心"          # 主导 = 最后一条
    assert meta["emotion_trace"] == ["有点开心", "有点安心"]
    assert meta["emotion_reasons"] == ["被信任", "感觉被依赖"]
    assert meta["emotion_match"] == "time"


def test_episode_node_dedupes_by_date():
    # 传入跨日期情绪时，只保留与 episode 日期一致的
    emotions = [
        _emo("开心", date="10月2日"),
        _emo("生气", date="10月3日"),
    ]
    node = _episode_node("chenxiao", _episode("e1"), 0, emotions)
    assert node["meta"]["emotion"] == "开心"
    assert node["meta"]["emotion_trace"] == ["开心"]


def test_episode_node_filters_empty_emotion_rows():
    emotions = [
        _emo("", reason=""),
        _emo("紧张", time="09:30", reason="第一次见他"),
    ]
    node = _episode_node("chenxiao", _episode("e1"), 0, emotions)
    assert node["meta"]["emotion"] == "紧张"
    assert node["meta"]["emotion_match"] == "time"


def test_episode_node_empty_when_no_emotions():
    node = _episode_node("chenxiao", _episode("e1"), 0, [])
    assert node["meta"]["emotion"] == ""
    assert node["meta"]["emotion_trace"] == []
    assert node["meta"]["emotion_reasons"] == []


def test_episode_node_default_day_emotions_is_none():
    # 向后兼容：不传情绪列表不报错
    node = _episode_node("chenxiao", _episode("e1"), 0)
    assert node["meta"]["emotion"] == ""
