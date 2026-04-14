"""对话编排流程。"""

import asyncio

from engine.agent_factory import get_choices_agent, get_state_updater_agent
from engine.agent_runner import run_structured_agent
from engine.agent_schema import ChoicesOutput, StateUpdaterOutput
from engine.character import get_character, narrator
from engine.prompt_builder import build_history_transcript
from llm.providers import get_choices_llm_config, get_narrator_llm_config
from log_config.routing import routing_logger
from shared.config import AGENT_RUN_TIMEOUT_SECONDS
from shared.text_utils import clean_response, is_valid_response, process_character_response
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


def _build_state_updater_input(
    user_input: str,
    scene_description: str,
    targets: list[str],
    agent_responses: list[tuple[str, str]],
) -> str:
    status_content = read_agent_file("narrator", "status.md")
    targets_text = ", ".join(targets) if targets else "无"
    response_blocks = [
        f"【{agent_name}】\n{response}" for agent_name, response in agent_responses if response
    ]
    responses_text = "\n\n".join(response_blocks) if response_blocks else "无"

    parts = [
        f"<current_narrator_status>\n{status_content}\n</current_narrator_status>",
        f"<player_input>\n{user_input}\n</player_input>",
        f"<narrator_targets>\n{targets_text}\n</narrator_targets>",
        f"<narrator_content>\n{scene_description or '无'}\n</narrator_content>",
        f"<agent_responses>\n{responses_text}\n</agent_responses>",
    ]
    return "\n\n---\n\n".join(parts)


async def run_state_updater(
    user_input: str,
    scene_description: str,
    targets: list[str],
    agent_responses: list[tuple[str, str]],
) -> None:
    """回合结束后维护 narrator 的状态和待触发事件。"""
    user_message = _build_state_updater_input(
        user_input, scene_description, targets, agent_responses
    )
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


async def call_narrator_and_route(user_input: str) -> tuple[list[str], str, bool]:
    """调用 narrator 获取路由决策和场景描述。"""
    return await narrator.route(user_input)


async def run_agent_in_scene(
    agent_name: str,
    targets: list[str],
    user_input: str,
) -> str | None:
    """在场景上下文中运行单个角色并广播响应。"""
    from storage.message_router import message_router

    output = await get_character(agent_name).run(user_input)
    response = process_character_response(clean_response(output.content))
    if is_valid_response(response, agent_name):
        await message_router.broadcast_agent_response(agent_name, targets, response)
    return response
