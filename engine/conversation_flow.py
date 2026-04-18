"""对话编排流程。"""

import asyncio

from engine.agent_factory import get_choices_agent, get_state_updater_agent
from engine.agent_runner import run_structured_agent
from engine.agent_schema import ChoicesOutput, NewCharacterSpec, StateUpdaterOutput
from engine.character import get_character, narrator
from engine.character_factory import create_character
from engine.prompt_builder import build_history_transcript
from engine.world_sync import post_turn_world_sync
from llm.providers import get_choices_llm_config, get_narrator_llm_config
from log_config.routing import routing_logger
from memory.parser import extract_status_field
from shared.config import AGENT_RUN_TIMEOUT_SECONDS, get_agent_names
from shared.text_utils import clean_response, get_display_name, is_valid_response, process_character_response, role_to_speaker
from storage.agent_files import read_agent_file
from storage.history import load_conversation_history


async def generate_choices(
    scene_description: str, agent_responses: list[tuple[str, str]]
) -> list[str]:
    """根据当前场景和角色回应生成玩家可选行动（2-3 个）。"""
    raw_messages = load_conversation_history(limit=None)
    parts: list[str] = []
    history, _ = build_history_transcript("narrator", raw_messages)
    if history:
        parts.append(f"【近期对话】\n{history}")
    if scene_description:
        parts.append(f"【场景】\n{scene_description}")
    for name, response in agent_responses:
        parts.append(f"【{name}】\n{response}")

    config = get_choices_llm_config()
    try:
        output = await run_structured_agent(
            agent=get_choices_agent(),
            user_input="\n\n".join(parts),
            output_type=ChoicesOutput,
            timeout_seconds=5,
            workflow_name="agentgal_choices",
            trace_metadata=None,
            usage_agent="choices",
            usage_phase="agent_run",
            model_name=config["model"],
        )
    except Exception:
        return []
    return output.choices[:3]


def _build_state_updater_input() -> str:
    character_intention = _format_character_intentions()
    status_content = read_agent_file("narrator", "status.md")
    raw_messages = load_conversation_history(limit=3)
    history_lines: list[str] = []
    for msg in raw_messages:
        role = msg.get("role", "unknown")
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        content = "\n".join(line.rstrip() for line in content.splitlines())
        if len(content) > 900:
            content = content[:900].rstrip() + "..."
        speaker = role_to_speaker(role)
        history_lines.append(f"{speaker}: {content}")
    recent_history = "\n\n".join(history_lines) if history_lines else "无"

    parts = [
        f"<character_intention>\n{character_intention}\n</character_intention>",
        f"<current_narrator_status>\n{status_content}\n</current_narrator_status>",
        f"<recent_history>\n{recent_history}\n</recent_history>",
    ]
    return "\n\n---\n\n".join(parts)


def _format_character_intentions() -> str:
    """提取所有角色的「打算」，供 state_updater 同步到公共待触发事件。"""
    blocks: list[str] = []
    for agent_name in get_agent_names(include_narrator=False):
        status_content = read_agent_file(agent_name, "status.md")
        intentions = extract_status_field(status_content, "打算").strip() or "（暂无）"
        soul_content = read_agent_file(agent_name, "soul.md")
        display_name = get_display_name(agent_name, soul_content)
        blocks.append(f"【{agent_name} / {display_name}】\n{intentions}")
    return "\n\n".join(blocks) if blocks else "无"


async def run_state_updater(targets: list[str]) -> None:
    """回合结束后维护 narrator 的状态和待触发事件，并同步角色位置。"""
    prev_status = read_agent_file("narrator", "status.md")
    prev_time = extract_status_field(prev_status, "当前时间").strip()
    prev_scene = extract_status_field(prev_status, "场景").strip()

    user_message = _build_state_updater_input()
    config = get_narrator_llm_config()
    try:
        output = await run_structured_agent(
            agent=get_state_updater_agent(),
            user_input=user_message,
            output_type=StateUpdaterOutput,
            timeout_seconds=AGENT_RUN_TIMEOUT_SECONDS,
            workflow_name="agentgal_state_update",
            trace_metadata={"agent_name": "state_updater"},
            usage_agent="state_updater",
            usage_phase="agent_run",
            model_name=config["model"],
        )
    except Exception as e:
        routing_logger.error(f"[state_updater] 运行失败: {e}")
        return
    await narrator.apply_state_updates(output)

    new_time = output.status.当前时间.strip() or prev_time
    new_scene = output.status.场景.strip() or prev_scene
    try:
        post_turn_world_sync(new_scene, targets, prev_time, new_time)
    except Exception as e:
        routing_logger.error(f"[world_sync] 同步位置失败: {e}")


async def call_narrator_and_route(
    user_input: str,
) -> tuple[list[str], str, list[NewCharacterSpec], bool]:
    """调用 narrator 获取路由决策、场景描述和新角色请求。"""
    return await narrator.route(user_input)


async def bootstrap_new_characters(
    specs: list[NewCharacterSpec],
    targets: list[str],
) -> tuple[list[str], list[str]]:
    """孵化 narrator 请求的新角色，并清理 targets 里的新角色引用。

    返回 (最新 targets, 成功创建的 agent_id 列表)。
    只有「本来就在 targets 里」且「孵化成功」的新角色，才会保留在本轮回应名单中；
    仅被创建但未被 narrator 点名的新角色，不会被强制拉进本轮发言。
    """
    if not specs:
        return targets, []

    created: list[str] = []
    requested = {spec.name for spec in specs}
    for spec in specs:
        ok = await create_character(spec)
        if ok:
            created.append(spec.name)

    created_set = set(created)
    filtered_targets = [
        target
        for target in targets
        if target not in requested or target in created_set
    ]
    return list(dict.fromkeys(filtered_targets)), created


async def run_agent_in_scene(
    agent_name: str,
    targets: list[str],
    user_input: str,
) -> str | None:
    """在场景上下文中运行单个角色并广播响应。"""
    from storage.message_router import message_router

    output = await get_character(agent_name).run(user_input, scene_targets=targets)
    response = process_character_response(clean_response(output.content))
    if is_valid_response(response, agent_name):
        await message_router.broadcast_agent_response(agent_name, targets, response)
    return response
