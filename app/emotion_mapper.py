"""表现层映射：情绪标签 → 可动模型表情参数。

- 情绪词表是开放的（prompt 给示例，LLM 自由发挥）；未命中词 → 默认平静参数
- 强度前缀（有点/很/非常…）剥离后查基础词，再对参数做缩放（clamp 0~1）
- 本表是纯数据 + 纯函数，供前端（Live2D/Spine 等）或后续渲染层消费

参数含义（约定为 0~1）：
    mouth_smile  嘴角（0 平 / 1 大幅上扬；<0 表示下垂）
    eye_open     眼睛开度（0 眯眼 / 1 瞪大）
    brow_anger   眉毛（0 平 / 1 紧皱）
    blush        脸红（0~1）
    action       推荐动作/姿势（字符串，供动画层选播）
"""

from __future__ import annotations

from typing import Any

# 基础情绪 → 表情参数（未列出的情绪走 DEFAULT_PARAMS）
EMOTION_PARAMS: dict[str, dict[str, Any]] = {
    "开心": {"mouth_smile": 0.8, "eye_open": 0.3, "brow_anger": 0.0, "blush": 0.0, "action": "挥手/蹦跳"},
    "喜悦": {"mouth_smile": 0.9, "eye_open": 0.4, "brow_anger": 0.0, "blush": 0.1, "action": "拍手"},
    "微笑": {"mouth_smile": 0.5, "eye_open": 0.2, "brow_anger": 0.0, "blush": 0.0, "action": "微笑"},
    "害羞": {"mouth_smile": 0.3, "eye_open": 0.1, "brow_anger": 0.0, "blush": 1.0, "action": "低头"},
    "脸红": {"mouth_smile": 0.2, "eye_open": 0.1, "brow_anger": 0.0, "blush": 1.0, "action": "侧过脸"},
    "撒娇": {"mouth_smile": 0.7, "eye_open": 0.6, "brow_anger": 0.0, "blush": 0.3, "action": "扯袖子"},
    "心动": {"mouth_smile": 0.6, "eye_open": 0.7, "brow_anger": 0.0, "blush": 0.8, "action": "捂住胸口"},
    "生气": {"mouth_smile": -0.5, "eye_open": 0.2, "brow_anger": 1.0, "blush": 0.0, "action": "抱臂"},
    "愤怒": {"mouth_smile": -0.7, "eye_open": 0.3, "brow_anger": 1.0, "blush": 0.0, "action": "握拳"},
    "吃醋": {"mouth_smile": -0.3, "eye_open": 0.3, "brow_anger": 0.6, "blush": 0.0, "action": "别过脸"},
    "难过": {"mouth_smile": -0.6, "eye_open": 0.1, "brow_anger": 0.0, "blush": 0.0, "action": "擦眼泪"},
    "委屈": {"mouth_smile": -0.4, "eye_open": 0.4, "brow_anger": 0.0, "blush": 0.0, "action": "抿嘴"},
    "落泪": {"mouth_smile": -0.6, "eye_open": 0.2, "brow_anger": 0.0, "blush": 0.0, "action": "低头抹泪"},
    "害怕": {"mouth_smile": -0.3, "eye_open": 1.0, "brow_anger": 0.4, "blush": 0.0, "action": "后退"},
    "惊讶": {"mouth_smile": 0.2, "eye_open": 1.0, "brow_anger": 0.2, "blush": 0.0, "action": "后退/捂嘴"},
    "震惊": {"mouth_smile": 0.1, "eye_open": 1.0, "brow_anger": 0.3, "blush": 0.0, "action": "定住"},
    "疑惑": {"mouth_smile": -0.1, "eye_open": 0.6, "brow_anger": 0.3, "blush": 0.0, "action": "歪头"},
    "无语": {"mouth_smile": -0.2, "eye_open": 0.2, "brow_anger": 0.2, "blush": 0.0, "action": "叹气"},
    "冷漠": {"mouth_smile": -0.2, "eye_open": 0.2, "brow_anger": 0.0, "blush": 0.0, "action": "不看对方"},
    "疲惫": {"mouth_smile": -0.1, "eye_open": 0.1, "brow_anger": 0.0, "blush": 0.0, "action": "揉眼睛"},
    "安心": {"mouth_smile": 0.4, "eye_open": 0.2, "brow_anger": 0.0, "blush": 0.0, "action": "放松"},
    "平静": {"mouth_smile": 0.1, "eye_open": 0.2, "brow_anger": 0.0, "blush": 0.0, "action": "微笑"},
}

# 默认（未命中词）：平静表情
DEFAULT_PARAMS: dict[str, Any] = dict(EMOTION_PARAMS["平静"])

# 强度前缀 → 参数缩放系数（clamp 0~1）
INTENSITY_PREFIXES: tuple[str, ...] = ("非常", "超级", "特别", "极其", "很", "有点", "有些", "稍微", "略")
INTENSITY_SCALE: dict[str, float] = {
    "非常": 1.6, "超级": 1.8, "特别": 1.4, "极其": 1.8,
    "很": 1.3, "有点": 0.6, "有些": 0.6, "稍微": 0.5, "略": 0.5,
}


def split_emotion(text: str) -> tuple[str, str]:
    """拆分强度前缀与基础情绪词。返回 (intensity, base_emotion)。

    "非常害羞" → ("非常", "害羞")；"开心" → ("", "开心")。
    """
    text = str(text).strip()
    for prefix in INTENSITY_PREFIXES:
        if text.startswith(prefix) and len(text) > len(prefix):
            return prefix, text[len(prefix):].strip()
    return "", text


def emotion_to_params(text: str) -> dict[str, Any]:
    """情绪标签 → 表情参数（带强度缩放）。未命中返回默认平静参数。"""
    intensity, base = split_emotion(text)
    params = EMOTION_PARAMS.get(base) or DEFAULT_PARAMS
    if not base:
        return dict(params)

    scale = INTENSITY_SCALE.get(intensity, 1.0)
    scaled: dict[str, Any] = {}
    for key, value in params.items():
        if key == "action":
            scaled[key] = value
        elif isinstance(value, (int, float)):
            scaled[key] = max(-1.0, min(1.0, float(value) * scale))
        else:
            scaled[key] = value
    return scaled
