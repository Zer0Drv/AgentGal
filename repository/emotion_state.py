"""内心层：角色三维驱动力状态（孤单 / 精力 / 亲近感）。

- mood.json 是本模块独占的运行时文件（LLM 永不直接写数值）
- 数值随真实时间演化（evolve），受对话内容影响（apply_impact 合并 emotional_impact delta）
- 提供泊松门控纯函数（gate_proactive），供未来主动联系 cron / 服务定时器调用
- hint_for_mood 把数值折叠成文字，供 prompt 注入（LLM 只读文字，不读数值）

借鉴 agent-emotion（yyh-001/agent-emotion，MIT）的演化与门控思路，
但情绪标签（表现层）单独归 emotion_store / emotion_mapper 管理，本模块只做驱动力。
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta
from typing import Any, Literal

from repository.config import character_path
from repository.log_config.routing import routing_logger

# 三维度名称
DIMS = ("loneliness", "energy", "affection")

# 初始默认状态
DEFAULT_MOOD: dict[str, float] = {"loneliness": 0.4, "energy": 0.65, "affection": 0.5}

# ---- 演化参数（借鉴 agent-emotion 的量级，可调） ----
LONELINESS_DRIFT_PER_HOUR = 0.05       # 空闲时孤单缓慢上升
LONELINESS_RECENT_WINDOW_H = 1.0       # 互动后 1 小时内孤单快速回落
LONELINESS_RECENT_DECAY_PER_HOUR = 0.25
ENERGY_DAY_RECOVER_PER_HOUR = 0.03     # 白天精力恢复
ENERGY_NIGHT_DROP_PER_HOUR = 0.08      # 夜晚精力下降
AFFECTION_DECAY_AFTER_HOURS = 6.0      # 6 小时无接触，亲近感开始衰减
AFFECTION_DECAY_PER_HOUR = 0.01

# ---- 互动基准回调（note_user） ----
NOTE_USER_DELTA: dict[str, float] = {"loneliness": -0.3, "affection": 0.1, "energy": 0.05}

# ---- emotional_impact 符号 → delta（LLM 只输出符号方向，幅度由代码控制） ----
_IMPACT_MAGNITUDE: dict[str, float] = {"++": 0.28, "+": 0.12, "0": 0.0, "-": -0.12, "--": -0.28}


def impact_delta(impact: dict[str, Any] | None) -> dict[str, float]:
    """把 LLM 输出的符号方向（affection/loneliness/energy）翻译成数值 delta。"""
    if not impact:
        return {dim: 0.0 for dim in DIMS}
    return {
        dim: _IMPACT_MAGNITUDE.get(str(impact.get(dim, "0")), 0.0)
        for dim in DIMS
    }

# ---- 泊松门控默认参数 ----
GATE_DEFAULTS: dict[str, Any] = {
    "awake_start_hour": 9,
    "awake_end_hour": 22,
    "daily_ping_limit": 4,
    "min_gap_minutes": 90,
    "base_lambda_per_hour": 0.15,
    "loneliness_weight": 0.45,
    "affection_weight": 0.2,
    "energy_floor": 0.25,
    "energy_scale": 0.9,
}

_STATE_FILENAME = "mood.json"


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _state_path(name: str) -> str:
    return character_path(name, _STATE_FILENAME)


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "mood": dict(DEFAULT_MOOD),
        "last_evolve_at": None,
        "last_interaction_at": None,
        "last_ping_at": None,
        "pings_today": 0,
        "pings_day": None,
        "next_ok_at": None,
        "force_wake": False,
    }


def load_state(name: str) -> dict[str, Any]:
    """读取 mood.json；不存在或损坏时返回默认状态（不写回）。"""
    path = _state_path(name)
    base = default_state()
    if not os.path.exists(path):
        return base
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return base
        for key in base:
            if key not in raw:
                raw[key] = base[key]
        return raw
    except (json.JSONDecodeError, OSError) as e:
        routing_logger.warning("[emotion_state] 读取失败 %s: %s", path, e)
        return base


def save_state(name: str, state: dict[str, Any]) -> None:
    path = _state_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# =============================================================================
# 纯演化逻辑
# =============================================================================


def evolve(
    state: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """按真实时间对三维状态做漂移，返回新 state（不落盘）。

    孤单：互动后 1h 内快速回落；否则缓慢上升。
    精力：白天恢复，夜晚下降（按当前时刻的时段判定）。
    亲近感：距上次互动超过 6h 后缓慢衰减。
    """
    now = now or _now()
    last_at = _parse_iso(state.get("last_evolve_at"))
    if last_at is None:
        state["last_evolve_at"] = now.isoformat()
        return state

    hours = max(0.0, (now - last_at).total_seconds() / 3600.0)
    if hours <= 0:
        return state

    mood = state.setdefault("mood", dict(DEFAULT_MOOD))
    lon = float(mood.get("loneliness", 0.5))
    ene = float(mood.get("energy", 0.5))
    aff = float(mood.get("affection", 0.5))

    # 孤单
    last_interaction = _parse_iso(state.get("last_interaction_at"))
    if last_interaction is not None and (now - last_interaction).total_seconds() / 3600.0 < LONELINESS_RECENT_WINDOW_H:
        lon -= LONELINESS_RECENT_DECAY_PER_HOUR * hours
    else:
        lon += LONELINESS_DRIFT_PER_HOUR * hours

    # 精力：按"演化期间所处时段"粗略判定——简单起见按结束时刻的时段
    hour = now.hour
    if 6 <= hour < 22:
        ene += ENERGY_DAY_RECOVER_PER_HOUR * hours
    else:
        ene -= ENERGY_NIGHT_DROP_PER_HOUR * hours

    # 亲近感
    if last_interaction is not None:
        gap_hours = (now - last_interaction).total_seconds() / 3600.0
        if gap_hours > AFFECTION_DECAY_AFTER_HOURS:
            aff -= AFFECTION_DECAY_PER_HOUR * (gap_hours - AFFECTION_DECAY_AFTER_HOURS)

    mood["loneliness"] = _clamp(lon)
    mood["energy"] = _clamp(ene)
    mood["affection"] = _clamp(aff)
    state["mood"] = {k: _clamp(float(mood.get(k, 0.5))) for k in DIMS}
    state["last_evolve_at"] = now.isoformat()
    return state


def apply_impact(
    state: dict[str, Any],
    impact: dict[str, Any] | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """合并 emotional_impact（LLM 输出的符号方向）到三维状态。

    impact 形如 {"affection": "+", "loneliness": "-", "energy": "0", "reason": "..."}。
    delta 由 app.emotion_mapper.impact_delta 提供；本模块只做 clamp 与落位。
    """
    now = now or _now()
    if not impact:
        return state

    delta = impact_delta(impact)
    mood = state.setdefault("mood", dict(DEFAULT_MOOD))
    for dim in DIMS:
        mood[dim] = _clamp(float(mood.get(dim, 0.5)) + delta.get(dim, 0.0))
    state["mood"] = {k: _clamp(float(mood.get(k, 0.5))) for k in DIMS}
    state["last_evolve_at"] = now.isoformat()
    return state


def note_user(
    state: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """玩家互动基准回调：互动本身带来的状态变化 + 记录互动时间。"""
    now = now or _now()
    mood = state.setdefault("mood", dict(DEFAULT_MOOD))
    for dim, delta in NOTE_USER_DELTA.items():
        mood[dim] = _clamp(float(mood.get(dim, 0.5)) + delta)
    state["mood"] = {k: _clamp(float(mood.get(k, 0.5))) for k in DIMS}
    state["last_interaction_at"] = now.isoformat()
    state["last_evolve_at"] = now.isoformat()
    return state


# =============================================================================
# 对外 API（读-演化-写回）
# =============================================================================


def refresh(name: str, now: datetime | None = None) -> dict[str, Any]:
    """读取并演化状态后写回，返回最新 state（用于回合开始 / prompt 注入前）。"""
    state = load_state(name)
    evolve(state, now)
    save_state(name, state)
    return state


def note_user_turn(name: str, impact: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """玩家回合结束回调：evolve → 合并 emotional_impact → 应用互动基准 → 写回。"""
    state = load_state(name)
    evolve(state, now)
    apply_impact(state, impact, now)
    note_user(state, now)
    save_state(name, state)
    return state


def build_inner_hint(name: str, now: datetime | None = None) -> str:
    """把当前内心状态折叠成 prompt 可读的中文提示（LLM 不读数值）。"""
    state = refresh(name, now)
    return hint_for_mood(state.get("mood") or DEFAULT_MOOD)


def hint_for_mood(mood: dict[str, float]) -> str:
    lon = float(mood.get("loneliness", 0.5))
    ene = float(mood.get("energy", 0.5))
    aff = float(mood.get("affection", 0.5))

    parts: list[str] = []
    if lon >= 0.75:
        parts.append("你有些孤单，隐隐希望有人陪")
    elif lon >= 0.6:
        parts.append("你有点想找人说话")
    if aff >= 0.7:
        parts.append("你和玩家很亲近，在一起时很安心")
    elif aff <= 0.3:
        parts.append("你和玩家的关系还有些生疏")
    if ene < 0.35:
        parts.append("你有些疲惫，没什么精力应付社交")
    elif ene >= 0.7:
        parts.append("你精力充沛，状态很好")
    if not parts:
        parts.append("你情绪平稳，状态如常")
    return "，".join(parts) + "。"


# =============================================================================
# 泊松门控（纯函数，供未来主动联系调度使用；当前版本不接 cron）
# =============================================================================


def _reset_daily_if_needed(state: dict[str, Any], now: datetime) -> None:
    day = now.date().isoformat()
    if state.get("pings_day") != day:
        state["pings_day"] = day
        state["pings_today"] = 0


def _in_awake_window(now: datetime, cfg: dict[str, Any]) -> bool:
    start = int(cfg.get("awake_start_hour", GATE_DEFAULTS["awake_start_hour"]))
    end = int(cfg.get("awake_end_hour", GATE_DEFAULTS["awake_end_hour"]))
    return start <= now.hour < end


def gate_proactive(name: str, cfg: dict[str, Any] | None = None, now: datetime | None = None) -> tuple[bool, str, dict[str, Any]]:
    """泊松门控判定：是否应该让角色主动联系玩家。

    返回 (should_wake, reason, state)。不修改任何外部副作用；写回由调用方负责。
    reason 取值：force_wake / quiet_hours / daily_limit / before_next_ok / poisson_miss / poisson_hit
    """
    cfg = {**GATE_DEFAULTS, **(cfg or {})}
    now = now or _now()
    state = load_state(name)
    evolve(state, now)
    _reset_daily_if_needed(state, now)

    if state.get("force_wake") is True:
        state["force_wake"] = False
        return True, "force_wake", state

    if not _in_awake_window(now, cfg):
        return False, "quiet_hours", state

    limit = int(cfg.get("daily_ping_limit", GATE_DEFAULTS["daily_ping_limit"]))
    if int(state.get("pings_today") or 0) >= limit:
        return False, "daily_limit", state

    next_ok = _parse_iso(state.get("next_ok_at"))
    if next_ok is not None and now < next_ok:
        return False, "before_next_ok", state

    if not _should_wake_probabilistic(state, now, cfg):
        return False, "poisson_miss", state

    return True, "poisson_hit", state


def _should_wake_probabilistic(state: dict[str, Any], now: datetime, cfg: dict[str, Any]) -> bool:
    """P(wake) = 1 - exp(-λ·Δt)，λ 由孤单/亲近感放大、精力缩放。"""
    mood = state.get("mood") or DEFAULT_MOOD
    lon = float(mood.get("loneliness", 0.5))
    aff = float(mood.get("affection", 0.5))
    ene = float(mood.get("energy", 0.5))

    base = float(cfg.get("base_lambda_per_hour", GATE_DEFAULTS["base_lambda_per_hour"]))
    lw = float(cfg.get("loneliness_weight", GATE_DEFAULTS["loneliness_weight"]))
    aw = float(cfg.get("affection_weight", GATE_DEFAULTS["affection_weight"]))
    floor = float(cfg.get("energy_floor", GATE_DEFAULTS["energy_floor"]))
    scale = float(cfg.get("energy_scale", GATE_DEFAULTS["energy_scale"]))

    lam = base * (1.0 + lw * lon + aw * aff)
    if ene < floor:
        lam *= max(0.05, ene / max(floor, 1e-6))
    else:
        lam *= scale * (0.5 + 0.5 * ene)
    lam = max(0.01, min(lam, 1.5))

    last_gate = _parse_iso(state.get("last_ping_at")) or _parse_iso(state.get("next_ok_at"))
    delta_hours = 1.0  # 默认按 1 小时窗口滚动一次判定
    if last_gate is not None:
        delta_hours = max(0.0, (now - last_gate).total_seconds() / 3600.0)
    p_wake = 1.0 - math.exp(-lam * max(delta_hours, 0.1))
    return _random() < p_wake


def _random() -> float:
    import random

    return random.random()


def mark_ping(name: str, now: datetime | None = None) -> dict[str, Any]:
    """主动消息发出后回调：记录 ping 时间/次数，并预约下一次最早可主动时间。"""
    cfg = GATE_DEFAULTS
    now = now or _now()
    state = load_state(name)
    _reset_daily_if_needed(state, now)
    mood = state.setdefault("mood", dict(DEFAULT_MOOD))
    mood["loneliness"] = _clamp(float(mood.get("loneliness", 0.5)) - 0.22)
    mood["affection"] = _clamp(float(mood.get("affection", 0.5)) + 0.05)
    mood["energy"] = _clamp(float(mood.get("energy", 0.5)) - 0.03)
    state["mood"] = {k: _clamp(float(mood.get(k, 0.5))) for k in DIMS}
    state["last_ping_at"] = now.isoformat()
    state["pings_today"] = int(state.get("pings_today") or 0) + 1
    state["next_ok_at"] = (now + timedelta(minutes=int(cfg.get("min_gap_minutes", 90)))).isoformat()
    save_state(name, state)
    return state
