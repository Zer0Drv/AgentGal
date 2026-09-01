"""情绪系统测试：emotion_mapper（表现层标签→表情参数）+ impact_delta（符号→delta）。"""

from app.emotion_mapper import (
    DEFAULT_PARAMS,
    emotion_to_params,
    split_emotion,
)
from repository.emotion_state import impact_delta


# ---- split_emotion ----


def test_split_emotion_plain():
    assert split_emotion("开心") == ("", "开心")


def test_split_emotion_with_intensity():
    assert split_emotion("非常害羞") == ("非常", "害羞")
    assert split_emotion("有点生气") == ("有点", "生气")
    assert split_emotion("很委屈") == ("很", "委屈")


def test_split_emotion_intensity_only_word():
    # 强度词本身就是完整情绪词时不应误拆（如"很"开头的罕见情绪词）
    assert split_emotion("很") == ("", "很")


# ---- emotion_to_params ----


def test_emotion_to_params_known_emotion():
    params = emotion_to_params("开心")
    assert params["mouth_smile"] == 0.8
    assert params["brow_anger"] == 0.0


def test_emotion_to_params_intensity_scales():
    base = emotion_to_params("开心")
    strong = emotion_to_params("非常开心")
    weak = emotion_to_params("有点开心")
    assert strong["mouth_smile"] > base["mouth_smile"] > weak["mouth_smile"]
    assert strong["mouth_smile"] <= 1.0


def test_emotion_to_params_unknown_falls_back_to_default():
    params = emotion_to_params("说不清的心情")
    assert params == DEFAULT_PARAMS
    assert params["mouth_smile"] == 0.1


def test_emotion_to_params_keeps_action_string():
    assert emotion_to_params("生气")["action"] == "抱臂"


# ---- impact_delta（符号 → 数值） ----


def test_impact_delta_empty_is_zero():
    assert impact_delta(None) == {"loneliness": 0.0, "energy": 0.0, "affection": 0.0}


def test_impact_delta_symbols():
    delta = impact_delta({"affection": "++", "loneliness": "--", "energy": "0"})
    assert delta["affection"] == 0.28
    assert delta["loneliness"] == -0.28
    assert delta["energy"] == 0.0


def test_impact_delta_unknown_symbol_ignored():
    delta = impact_delta({"affection": "?", "loneliness": "+"})
    assert delta["affection"] == 0.0
    assert delta["loneliness"] == 0.12
