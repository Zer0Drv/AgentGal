"""对话编排流程。"""

import asyncio
import re

from engine.agent_factory import get_choices_agent, get_conversation_agent
from engine.agent_runner import run_structured_agent
from engine.agent_schema import CharacterOutput, ChoicesOutput, NarratorOutput
from engine.prompt_builder import build_history_transcript, build_search_query, build_user_message
from storage.history import load_conversation_history
from llm.providers import get_choices_llm_config, get_llm_config, get_narrator_llm_config
from log_config.routing import routing_logger
from memory.retrieval import search_memories
from shared.config import AGENT_RUN_TIMEOUT_SECONDS, get_agent_names
from shared.text_utils import (
    clean_response,
    get_display_name,
    is_valid_response,
    process_character_response,
)
from storage.agent_files import (
    add_pending_event,
    mark_event_triggered,
    read_agent_file,
    update_memory,
    update_player,
    update_status,
)


async def _apply_response_updates(
    agent_name: str,
    output: CharacterOutput | NarratorOutput,
) -> None:
    """将 typed output 的更新指令写回对应文件。"""
    results: list[str] = []

    def _safe_update(label: str, fn) -> None:
        try:
            results.append(f"{label}: {fn()}")
        except Exception as e:
            routing_logger.error(f"[{agent_name}] {label} 失败: {e}")

    if isinstance(output, CharacterOutput) and output.memory:
        _safe_update("memory", lambda: update_memory(agent_name, output.memory))

    for field, content in output.status.items():
        _safe_update(
            f"status[{field}]",
            lambda field_name=field, field_content=content: update_status(
                agent_name, field_name, str(field_content)
            ),
        )

    if isinstance(output, CharacterOutput):
        for field, content in output.player.items():
            _safe_update(
                f"player[{field}]",
                lambda field_name=field, field_content=content: update_player(
                    agent_name, field_name, str(field_content)
                ),
            )

    event_section = "待触发事件" if agent_name == "narrator" else "打算"

    for event_name in output.triggered:
        _safe_update(
            f"triggered[{event_name}]",
            lambda triggered_name=event_name: mark_event_triggered(
                agent_name, triggered_name, event_section
            ),
        )

    for event_desc in output.add_event:
        if event_desc.strip() == "无":
            continue
        _safe_update(
            "add_event",
            lambda event_content=event_desc: add_pending_event(
                agent_name, event_content, event_section
            ),
        )

    if results:
        routing_logger.info(f"[{agent_name}] 文件更新: {'; '.join(results)}")


def _sanitize_narrator_scene_description(scene_description: str) -> str:
    """截断 narrator 输出中混入的角色发言，避免写入错误 canon。"""
    sanitized = clean_response(scene_description)
    valid_agents = get_agent_names(include_narrator=False)

    for agent_name in valid_agents:
        soul_content = read_agent_file(agent_name, "soul.md")
        names_to_check = [agent_name]
        display_name = get_display_name(agent_name, soul_content) if soul_content else None
        if display_name and display_name != agent_name:
            names_to_check.append(display_name)

        for name in names_to_check:
            for pattern in [
                re.compile(rf"^{re.escape(name)}\s*[:：]", re.MULTILINE | re.IGNORECASE),
                re.compile(rf"^##\s*{re.escape(name)}", re.MULTILINE | re.IGNORECASE),
            ]:
                match = pattern.search(sanitized)
                if match:
                    routing_logger.warning(
                        f"[narrator] 场景描述中检测到角色台词 '{name}'，已截断"
                    )
                    sanitized = sanitized[: match.start()].strip()
                    break

    return sanitized


def _log_narrator_failure(stage: str, exc: Exception) -> None:
    if isinstance(exc, asyncio.TimeoutError):
        routing_logger.error(f"[narrator] {stage} 超时: {exc}")
        return
    routing_logger.error(f"[narrator] {stage} 失败: {exc}")


async def _run_conversation_agent(
    agent_name: str,
    latest_user_input: str,
    raw_messages: list[dict] | None = None,
) -> CharacterOutput | NarratorOutput:
    relevant_memories = (
        search_memories(agent_name, build_search_query(agent_name, latest_user_input))
        if agent_name != "narrator"
        else ""
    )
    memory_prefix = (
        f"<relevant_memories>\n{relevant_memories}\n</relevant_memories>"
        if relevant_memories
        else ""
    )
    user_message = build_user_message(
        agent_name,
        latest_user_input,
        memory_prefix,
        raw_messages=raw_messages,
    )

    config = get_narrator_llm_config() if agent_name == "narrator" else get_llm_config()
    output_type = NarratorOutput if agent_name == "narrator" else CharacterOutput
    output = await run_structured_agent(
        agent=get_conversation_agent(agent_name),
        user_input=user_message,
        output_type=output_type,
        timeout_seconds=AGENT_RUN_TIMEOUT_SECONDS,
        workflow_name="agentgal_turn",
        trace_metadata={"agent_name": agent_name},
        usage_agent=agent_name,
        usage_phase="agent_run",
        model_name=config["model"],
    )
    await _apply_response_updates(agent_name, output)
    return output


async def generate_choices(
    scene_description: str, agent_responses: list[tuple[str, str]]
) -> list[str]:
    """根据当前场景和角色回应生成玩家可选行动（2-3 个）。"""
    raw_messages = load_conversation_history(limit=None)
    parts: list[str] = []
    history = build_history_transcript("narrator", raw_messages)
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
            timeout_seconds=30,
            workflow_name="agentgal_choices",
            trace_metadata=None,
            usage_agent="choices",
            usage_phase="agent_run",
            model_name=config["model"],
        )
    except Exception as e:
        routing_logger.warning(f"选项生成失败: {e}")
        return []
    return output.choices[:3]


async def call_narrator_and_route(
    user_input: str,
) -> tuple[list[str], str, bool]:
    """调用 narrator 获取路由决策和场景描述。"""
    valid_agents = get_agent_names(include_narrator=False)
    raw_messages = load_conversation_history(limit=None)

    async def _run_narrator() -> tuple[list[str], str]:
        output = await _run_conversation_agent("narrator", user_input, raw_messages=raw_messages)
        assert isinstance(output, NarratorOutput)
        valid_targets = [target for target in output.targets if target in valid_agents]
        scene_description = _sanitize_narrator_scene_description(output.content)
        return valid_targets, scene_description

    try:
        targets, scene_description = await _run_narrator()
    except Exception as e:
        _log_narrator_failure("首次调用", e)
        return [], "", False

    if not targets:
        routing_logger.warning("narrator 响应缺少有效 TARGETS，重试中...")
        try:
            targets, scene_description = await _run_narrator()
        except Exception as e:
            _log_narrator_failure("重试调用", e)
            return [], "", False
        if not targets:
            routing_logger.warning("narrator 重试后仍缺少有效 TARGETS")
            return [], "", False

    routing_logger.info(f"narrator 决定 targets: {targets}")
    return targets, scene_description, is_valid_response(scene_description, "narrator")


async def run_agent_in_scene(
    agent_name: str,
    targets: list[str],
    user_input: str,
) -> str | None:
    """在场景上下文中运行单个角色并广播响应。"""
    from storage.message_router import message_router

    raw_messages = load_conversation_history(limit=None)
    try:
        output = await _run_conversation_agent(agent_name, user_input, raw_messages=raw_messages)
    except Exception as e:
        routing_logger.error(f"[{agent_name}] run_agent 失败: {e}")
        return None

    assert isinstance(output, CharacterOutput)
    response = process_character_response(clean_response(output.content))
    if is_valid_response(response, agent_name):
        await message_router.broadcast_agent_response(agent_name, targets, response)
    return response
