"""每个角色的 schedule.json 解析与查询（纯函数，便于单测）。

每个角色在 data/characters/{name}/schedule.json 自维护一份作息。
系统只在时段切换时用 schedule 的默认值刷新"无打算"角色的 status.md.当前位置；
status.md.当前位置 始终是现时真相。
"""

from __future__ import annotations

import re

from engine.agent_schema import (
    CharacterSchedule,
    CharacterSchedulePeriod,
    CharacterScheduleSlot,
)
from shared.config import get_agent_names
from storage.agent_files import read_sidecar_json, write_sidecar_json


WEEKDAY_CN_TO_EN = {
    "一": "mon",
    "二": "tue",
    "三": "wed",
    "四": "thu",
    "五": "fri",
    "六": "sat",
    "日": "sun",
    "天": "sun",
}

TIME_BUCKETS = ("上午", "下午", "晚上", "全天")


# ---------------------------------------------------------------------------
# 加载与保存
# ---------------------------------------------------------------------------


def load_character_schedule(agent: str) -> CharacterSchedule:
    """加载角色 schedule.json；不存在或为空时返回空 Schedule。"""
    raw = read_sidecar_json(agent, "schedule.json")
    if not raw:
        return CharacterSchedule()
    return CharacterSchedule.model_validate(raw)


def load_all_schedules(include_narrator: bool = False) -> dict[str, CharacterSchedule]:
    """读入所有角色的 schedule（narrator 默认不参与）。"""
    return {
        name: load_character_schedule(name)
        for name in get_agent_names(include_narrator=include_narrator)
    }


def save_character_schedule(agent: str, schedule: CharacterSchedule) -> None:
    """写回角色 schedule.json（动态生成角色 / 人工编辑时使用）。"""
    write_sidecar_json(agent, "schedule.json", schedule.model_dump())


# ---------------------------------------------------------------------------
# 时间解析
# ---------------------------------------------------------------------------


def parse_game_time(game_time: str) -> tuple[str, str] | None:
    """解析 narrator `当前时间` 字段。

    输入示例: "4月3日 星期一 8:23" / "10月2日 星期一 09:20" / "星期日 晚上"
    返回 (weekday, time_bucket)；任一成分缺失时返回 None。
    """
    if not game_time:
        return None

    weekday_match = re.search(r"星期([一二三四五六日天])", game_time)
    if not weekday_match:
        return None
    weekday = WEEKDAY_CN_TO_EN[weekday_match.group(1)]

    bucket: str | None = None
    for candidate in TIME_BUCKETS:
        if candidate in game_time:
            bucket = candidate
            break

    if bucket is None:
        hour_match = re.search(r"(\d{1,2})\s*[:：]", game_time)
        if hour_match:
            hour = int(hour_match.group(1))
            if 6 <= hour < 12:
                bucket = "上午"
            elif 12 <= hour < 18:
                bucket = "下午"
            else:
                bucket = "晚上"

    if bucket is None:
        return None
    return weekday, bucket


def detect_slot_change(prev_time: str, now_time: str) -> bool:
    """判断两个 game_time 是否跨越了 slot 边界。"""
    prev = parse_game_time(prev_time)
    now = parse_game_time(now_time)
    return prev != now


# ---------------------------------------------------------------------------
# 单角色查询
# ---------------------------------------------------------------------------


def _period_covers_date(period: CharacterSchedulePeriod, date: str) -> bool:
    if not date:
        return True
    return period.start <= date <= period.end


def _match_slot(
    slots: list[CharacterScheduleSlot], weekday: str, bucket: str
) -> CharacterScheduleSlot | None:
    """显式 bucket 优先，'全天' 兜底。"""
    fallback: CharacterScheduleSlot | None = None
    for slot in slots:
        if weekday not in slot.days:
            continue
        if slot.time == bucket:
            return slot
        if slot.time == "全天" and fallback is None:
            fallback = slot
    return fallback


def find_slot(
    schedule: CharacterSchedule,
    slot_key: tuple[str, str],
    date: str = "",
) -> CharacterScheduleSlot | None:
    weekday, bucket = slot_key
    for period in schedule.periods:
        if not _period_covers_date(period, date):
            continue
        slot = _match_slot(period.slots, weekday, bucket)
        if slot is not None:
            return slot
    return None


def get_default_location(
    agent: str, slot_key: tuple[str, str], date: str = ""
) -> str | None:
    """返回 agent 在指定时段的默认位置；未配置返回 None。"""
    schedule = load_character_schedule(agent)
    slot = find_slot(schedule, slot_key, date)
    return slot.location if slot else None


# ---------------------------------------------------------------------------
# 跨角色聚合查询
# ---------------------------------------------------------------------------


def collect_default_locations(
    slot_key: tuple[str, str],
    date: str = "",
    agents: list[str] | None = None,
) -> dict[str, str]:
    """收集所有（或指定）角色在当前 slot 的默认位置。"""
    names = agents if agents is not None else get_agent_names(include_narrator=False)
    result: dict[str, str] = {}
    for name in names:
        loc = get_default_location(name, slot_key, date)
        if loc:
            result[name] = loc
    return result


def query_all_locations(
    slot_key: tuple[str, str],
    real_locations: dict[str, str],
    date: str = "",
) -> dict[str, list[str]]:
    """生成 location → [agents] 反向映射。

    real_locations 优先（现时真相）；schedule 补默认；都无则跳过。
    """
    merged = collect_default_locations(slot_key, date)
    for agent, loc in real_locations.items():
        if loc:
            merged[agent] = loc

    inverted: dict[str, list[str]] = {}
    for agent, loc in merged.items():
        if not loc:
            continue
        inverted.setdefault(loc, []).append(agent)
    for names in inverted.values():
        names.sort()
    return inverted


def query_who_is_here(
    location: str,
    slot_key: tuple[str, str],
    real_locations: dict[str, str],
    date: str = "",
) -> list[str]:
    """返回指定地点此时段的所有角色。"""
    return query_all_locations(slot_key, real_locations, date).get(location, [])
