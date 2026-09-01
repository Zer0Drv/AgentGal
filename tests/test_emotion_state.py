"""情绪系统测试：emotion_state（内心层：演化 / 回调 / 门控 / hint）。"""

from datetime import datetime, timedelta

import pytest

import repository.emotion_state as es
from repository.emotion_state import (
    default_state,
    evolve,
    gate_proactive,
    hint_for_mood,
    note_user,
    refresh,
)


def _make_state(**overrides):
    state = default_state()
    state.update(overrides)
    return state


def _dt(hour: int, minute: int = 0, day: int = 1) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=datetime.now().astimezone().tzinfo)


# ---- evolve ----


def test_evolve_loneliness_drifts_up_when_idle():
    state = _make_state(last_evolve_at=_dt(8).isoformat())
    evolved = evolve(state, now=_dt(10))
    assert evolved["mood"]["loneliness"] > 0.4
    assert evolved["last_evolve_at"] == _dt(10).isoformat()


def test_evolve_loneliness_drops_after_recent_interaction():
    state = _make_state(
        last_evolve_at=_dt(8).isoformat(),
        last_interaction_at=_dt(9).isoformat(),
    )
    evolved = evolve(state, now=_dt(9, 30))
    # 互动后 1h 内：孤单回落（0.4 - 0.25 * 1.5）
    assert evolved["mood"]["loneliness"] < 0.4


def test_evolve_energy_recovers_by_day_drops_at_night():
    day = evolve(_make_state(last_evolve_at=_dt(8).isoformat()), now=_dt(12))
    assert day["mood"]["energy"] > 0.65

    night = evolve(_make_state(last_evolve_at=_dt(20).isoformat()), now=_dt(23))
    assert night["mood"]["energy"] < 0.65


def test_evolve_affection_decays_only_after_six_hours():
    recent = _make_state(last_interaction_at=_dt(5).isoformat(), last_evolve_at=_dt(5).isoformat())
    evolved = evolve(recent, now=_dt(10))
    assert evolved["mood"]["affection"] == 0.5  # 距上次互动 5h < 6h，不衰减

    old = _make_state(last_interaction_at=_dt(0).isoformat(), last_evolve_at=_dt(0).isoformat())
    evolved_old = evolve(old, now=_dt(12))
    assert evolved_old["mood"]["affection"] < 0.5


def test_evolve_noop_without_elapsed_time():
    state = _make_state(last_evolve_at=_dt(10).isoformat())
    evolved = evolve(state, now=_dt(10))
    assert evolved["mood"] == state["mood"]


# ---- note_user / apply_impact / note_user_turn ----


def test_note_user_applies_base_delta():
    state = _make_state()
    noted = note_user(state, now=_dt(12))
    assert noted["mood"]["loneliness"] == pytest.approx(0.1)  # 0.4 - 0.3
    assert noted["mood"]["affection"] == pytest.approx(0.6)  # 0.5 + 0.1
    assert noted["last_interaction_at"] is not None


def test_apply_impact_merges_symbol_delta():
    state = _make_state()
    es.apply_impact(state, {"affection": "++", "loneliness": "--", "energy": "0"}, now=_dt(12))
    assert state["mood"]["affection"] == 0.78  # 0.5 + 0.28
    assert state["mood"]["loneliness"] == 0.12  # 0.4 - 0.28
    assert state["mood"]["energy"] == 0.65


def test_note_user_turn_combines_impact_and_base(tmp_path, monkeypatch):
    def fake_path(name, *subpaths):
        return str(tmp_path / name / "/".join(subpaths))

    monkeypatch.setattr(es, "character_path", fake_path)

    state = es.note_user_turn("mitsuki", {"affection": "+", "loneliness": "-", "energy": "0"})
    # 基准：孤单-0.3 亲近感+0.1 精力+0.05；impact：亲近感+0.12 孤单-0.12
    # 孤单 0.4-0.12-0.3 = -0.02 → clamp 到 0
    assert state["mood"]["affection"] == pytest.approx(0.5 + 0.1 + 0.12)
    assert state["mood"]["loneliness"] == pytest.approx(0.0)
    assert state["mood"]["energy"] == pytest.approx(0.65 + 0.05)

    # 落盘可读
    loaded = es.load_state("mitsuki")
    assert loaded["mood"] == state["mood"]


def test_refresh_persists_evolved_state(tmp_path, monkeypatch):
    def fake_path(name, *subpaths):
        return str(tmp_path / name / "/".join(subpaths))

    monkeypatch.setattr(es, "character_path", fake_path)
    state = refresh("mitsuki", now=_dt(12))
    assert "last_evolve_at" in state
    assert es.load_state("mitsuki")["mood"] == state["mood"]


# ---- hint_for_mood ----


def test_hint_for_mood_lonely_and_energetic():
    hint = hint_for_mood({"loneliness": 0.8, "energy": 0.8, "affection": 0.5})
    assert "孤单" in hint
    assert "精力充沛" in hint


def test_hint_for_mood_low_energy():
    hint = hint_for_mood({"loneliness": 0.3, "energy": 0.2, "affection": 0.5})
    assert "疲惫" in hint


def test_hint_for_mood_close_relationship():
    hint = hint_for_mood({"loneliness": 0.3, "energy": 0.5, "affection": 0.9})
    assert "亲近" in hint


def test_hint_for_mood_default_flat():
    hint = hint_for_mood({"loneliness": 0.5, "energy": 0.5, "affection": 0.5})
    assert "情绪平稳" in hint


# ---- gate_proactive ----


def test_gate_quiet_hours_rejects():
    now = _dt(23)  # 23 点，安静时段
    wake, reason, _ = gate_proactive("mitsuki", now=now)
    assert not wake
    assert reason == "quiet_hours"


def test_gate_daily_limit_rejects():
    now = _dt(12)
    cfg = {"daily_ping_limit": 1}
    state = _make_state(pings_today=1, pings_day=now.date().isoformat())
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(es, "load_state", lambda name: state)
    monkeypatch.setattr(es, "save_state", lambda name, s: None)
    try:
        wake, reason, _ = gate_proactive("mitsuki", cfg=cfg, now=now)
        assert not wake
        assert reason == "daily_limit"
    finally:
        monkeypatch.undo()


def test_gate_force_wake():
    now = _dt(12)
    state = _make_state(force_wake=True)
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(es, "load_state", lambda name: state)
    monkeypatch.setattr(es, "save_state", lambda name, s: None)
    try:
        wake, reason, _ = gate_proactive("mitsuki", now=now)
        assert wake
        assert reason == "force_wake"
    finally:
        monkeypatch.undo()


def test_gate_poisson_miss_when_loneliness_low():
    now = _dt(12)
    # 低孤单 + 门控在 1h 窗口内 → λ 小，命中概率低。固定随机为 0.999 保证 miss
    state = _make_state(
        mood={"loneliness": 0.0, "energy": 0.9, "affection": 0.5},
        pings_day=now.date().isoformat(),
        pings_today=0,
    )
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(es, "load_state", lambda name: state)
    monkeypatch.setattr(es, "save_state", lambda name, s: None)
    monkeypatch.setattr(es, "_random", lambda: 0.999)
    try:
        wake, reason, _ = gate_proactive("mitsuki", now=now)
        assert not wake
        assert reason == "poisson_miss"
    finally:
        monkeypatch.undo()


def test_mark_ping_records_and_reserves_next():
    tmp = __import__("tempfile").mkdtemp()

    def fake_path(name, *subpaths):
        return f"{tmp}/{name}/{'/'.join(subpaths)}"

    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(es, "character_path", fake_path)
    try:
        state = es.mark_ping("mitsuki", now=_dt(12))
        assert state["pings_today"] == 1
        assert state["mood"]["loneliness"] < 0.4
        assert state["next_ok_at"] is not None
    finally:
        monkeypatch.undo()
