"""对话编排流程。

职责：UI 适配 + 一次性编排。具体的路由 / 状态更新 / 记忆写回都封装在
Narrator / Character 实体方法里，这里只负责把编排串起来。
"""

from agents.factory import get_choices_agent
from agents.runner import run_structured_agent
from agents.schema import ChoicesOutput, NewCharacterRequest
from engine.character import get_character, narrator
from engine.character_factory import CreatedCharacterInfo, create_character
from engine.prompt_builder import build_history_transcript
from llm.providers import get_choices_llm_config
from log_config.routing import routing_logger
from shared.text_utils import clean_response, is_valid_response, process_character_response
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


async def bootstrap_new_characters(
    specs: list[NewCharacterRequest],
    targets: list[str],
) -> tuple[list[str], list[CreatedCharacterInfo]]:
    """孵化 narrator 请求的新角色，并把成功创建的角色补入最终 targets。

    返回 (最新 targets, 成功创建的角色信息列表)。
    """
    if not specs:
        return targets, []

    created: list[CreatedCharacterInfo] = []
    for spec in specs:
        created_info = await create_character(spec, scene_characters=targets)
        if created_info:
            created.append(created_info)

    created_ids = [info.character_id for info in created]
    missing_created = [character_id for character_id in created_ids if character_id not in targets]
    for character_id in missing_created:
        routing_logger.info(
            "[bootstrap_new_characters] 新角色 %s 创建成功，已补入最终 targets",
            character_id,
        )
    return list(dict.fromkeys(targets + created_ids)), created


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
