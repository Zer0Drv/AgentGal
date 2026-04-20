"""离场追补：角色重新登场时懒求值地补录一段离场期间的记忆。

不实时模拟，只在 Character.run() 入口处判断：若 `.last_seen.json` 距离
当前游戏时间超过阈值，就调 offstage_synthesizer agent 合成一条压缩记忆，
直接追加到角色 memory.md。追补失败只记日志，不打断本轮对话。
"""

from __future__ import annotations

from agents.factory import get_offstage_synthesizer_agent
from agents.runner import run_structured_agent
from agents.schema import OffstageMemoryBlock
from world.schedule import load_character_schedule
from llm.providers import get_offstage_synthesizer_llm_config
from log_config.routing import routing_logger
from memory.parser import extract_status_field, game_day_diff
from shared.config import AGENT_RUN_TIMEOUT_SECONDS, OFFSTAGE_CATCHUP_THRESHOLD_DAYS
from storage.agent_files import (
    read_agent_file,
    read_sidecar_json,
    update_memory,
    write_sidecar_json,
)


_LAST_SEEN_FILE = ".last_seen.json"


def _build_user_message(
    agent_name: str,
    last_seen: str,
    now_time: str,
    schedule_json: str,
    intentions: str,
    soul: str,
) -> str:
    return (
        f"<agent>{agent_name}</agent>\n\n"
        f"<soul>\n{soul.strip()}\n</soul>\n\n"
        f"<my_schedule>\n{schedule_json}\n</my_schedule>\n\n"
        f"<offstage_start>{last_seen}</offstage_start>\n"
        f"<offstage_end>{now_time}</offstage_end>\n\n"
        f"<intentions_snapshot>\n{intentions}\n</intentions_snapshot>"
    )


async def maybe_synthesize_offstage(agent_name: str, now_time: str) -> None:
    """若离场间隔超过阈值，合成一段离场记忆追加到 memory.md。

    首次出场（无 sidecar）时仅写入 last_seen，不追补；失败只记日志。
    """
    if not now_time:
        return

    sidecar = read_sidecar_json(agent_name, _LAST_SEEN_FILE)
    last_seen = (sidecar.get("last_seen") or "").strip()
    if not last_seen:
        write_sidecar_json(agent_name, _LAST_SEEN_FILE, {"last_seen": now_time})
        return

    gap = game_day_diff(now_time, last_seen)
    if gap is None or gap < OFFSTAGE_CATCHUP_THRESHOLD_DAYS:
        return

    soul = read_agent_file(agent_name, "soul.md")
    status_content = read_agent_file(agent_name, "status.md")
    intentions = extract_status_field(status_content, "打算").strip() or "（无明确打算）"
    schedule = load_character_schedule(agent_name)
    schedule_json = schedule.model_dump_json(indent=2) if schedule.periods else "{}"

    message = _build_user_message(
        agent_name, last_seen, now_time, schedule_json, intentions, soul
    )

    config = get_offstage_synthesizer_llm_config()
    try:
        block = await run_structured_agent(
            agent=get_offstage_synthesizer_agent(),
            user_input=message,
            output_type=OffstageMemoryBlock,
            timeout_seconds=AGENT_RUN_TIMEOUT_SECONDS,
            workflow_name="agentgal_offstage_synth",
            trace_metadata={"agent_name": "offstage_synthesizer", "target": agent_name},
            usage_agent="offstage_synthesizer",
            usage_phase="agent_run",
            model_name=config["model"],
        )
    except Exception as e:
        routing_logger.error(f"[offstage_synth] {agent_name!r} 追补失败: {e}")
        return

    date = block.date.strip()
    content = block.content.strip()
    if not date or not content:
        routing_logger.warning(f"[offstage_synth] {agent_name!r} 返回空 block，跳过")
        return

    memory_block = f"## {date}\n{content}"
    try:
        update_memory(agent_name, memory_block)
    except Exception as e:
        routing_logger.error(f"[offstage_synth] {agent_name!r} 写入 memory.md 失败: {e}")
        return

    routing_logger.info(
        f"[offstage_synth] {agent_name!r} 追补 {last_seen} → {now_time}（{gap} 天）"
    )
