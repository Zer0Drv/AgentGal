"""Agent 运行器 - 按需构建 system prompt 并调用 LLM"""

import asyncio
import re
import time

from shared.config import (
    AGENT_RUN_TIMEOUT_SECONDS,
    PROJECT_ROOT,
    get_agent_names,
)
from engine.response_parser import parse_agent_response
from shared.text_utils import clean_response, get_display_name, is_valid_response, process_character_response
from llm.llm_parser import OpenAICompatibleClient
from llm.providers import get_choices_llm_config, get_llm_config, get_narrator_llm_config
from log_config.agent_calls import log_agent_call
from log_config.routing import routing_logger
from storage.agent_files import (
    add_pending_event,
    get_allowed_fields,
    mark_event_triggered,
    read_agent_file,
    update_memory,
    update_player,
    update_status,
)
from engine.history import build_history_transcript, load_conversation_history
from memory.retrieval import search_memories


# ---------------------------------------------------------------------------
# Agent 构建
# ---------------------------------------------------------------------------

def _build_system_prompt(agent_name: str, soul_content: str) -> str:
    """构建 system prompt（仅包含稳定的身份与规则部分）。"""
    prompt_name = "narrator_prompt.txt" if agent_name == "narrator" else "character_prompt.txt"
    prompt_template = (PROJECT_ROOT / "prompts" / prompt_name).read_text(encoding="utf-8")
    # 「打算」由 <triggered>/<add_event> 专用标签管理，不暴露给 <status> 覆盖
    _status_excluded = {"打算"} if agent_name != "narrator" else set()
    status_fields = "、".join(f for f in get_allowed_fields(agent_name, "status") if f not in _status_excluded)
    player_fields = "、".join(get_allowed_fields(agent_name, "user")) if agent_name != "narrator" else ""
    display_name = get_display_name(agent_name, soul_content)

    characters = get_agent_names(include_narrator=False)
    characters_scene_list = "\n".join(
        f"- {get_display_name(c, read_agent_file(c, 'soul.md'))}：[位置] 或 不在场"
        for c in characters
    )
    valid_targets = ", ".join(characters)

    return prompt_template.format(
        agent_name=agent_name,
        display_name=display_name,
        soul=soul_content,
        status_fields=status_fields,
        player_fields=player_fields,
        characters_scene_list=characters_scene_list,
        valid_targets=valid_targets,
    )



def _build_user_message(
    agent_name: str,
    latest_user_input: str,
    memory_prefix: str,
    raw_messages: list[dict] | None = None,
) -> str:
    """构建单条大 user message，按稳定度排序上下文。

    parts 顺序：
    - `<growth>`
    - `<user_profile>`
    - 最近对话历史
    - `<status>`
    - `memory_prefix`（`<relevant_memories>`）
    - 本轮玩家输入

    narrator 不包含 `growth`、`user_profile` 和长期记忆召回块。
    """
    parts: list[str] = []

    growth_content: str = (read_agent_file(agent_name, "growth.md")) if agent_name != "narrator" else ""
    user_content: str = read_agent_file(agent_name, "user.md") if agent_name != "narrator" else ""
    history: str = build_history_transcript(agent_name, raw_messages or [])
    status_content: str = read_agent_file(agent_name, "status.md")

    parts.append(f"<growth>\n{growth_content.strip()}\n</growth>" if growth_content else "")
    parts.append(f"<user_profile>\n{user_content.strip()}\n</user_profile>" if user_content else '')
    parts.append(f"最近对话历史:\n\n{history}" if history else "")
    parts.append(f"<status>\n{status_content}\n</status>" if status_content else '')  
    parts.append(memory_prefix if memory_prefix else '')
    parts.append(f"玩家新消息: {latest_user_input}")

    return "\n\n---\n\n".join(parts)



# ---------------------------------------------------------------------------
# 响应后处理：写回文件
# ---------------------------------------------------------------------------

async def _apply_response_updates(agent_name: str, parsed) -> None:
    """将解析后的 XML 更新指令写回对应文件。"""
    results: list[str] = []

    def _safe_update(label: str, fn) -> None:
        """执行单次更新操作，捕获异常并记录结果。"""
        try:
            results.append(f"{label}: {fn()}")
        except Exception as e:
            routing_logger.error(f"[{agent_name}] {label} 失败: {e}")

    if parsed.memory:
        _safe_update("memory", lambda: update_memory(agent_name, parsed.memory))

    if parsed.status:
        for field, content in parsed.status.items():
            _safe_update(f"status[{field}]", lambda f=field, c=content: update_status(agent_name, f, str(c)))

    if parsed.player:
        for field, content in parsed.player.items():
            _safe_update(f"player[{field}]", lambda f=field, c=content: update_player(agent_name, f, str(c)))

    # narrator 操作「待触发事件」，其他角色操作「打算」
    event_section = "待触发事件" if agent_name == "narrator" else "打算"

    if parsed.triggered:
        for event_name in parsed.triggered:
            _safe_update(f"triggered[{event_name}]", lambda n=event_name: mark_event_triggered(agent_name, n, event_section))

    if parsed.add_event:
        for event_desc in parsed.add_event:
            if event_desc.strip() == "无":
                continue
            _safe_update("add_event", lambda d=event_desc: add_pending_event(agent_name, d, event_section))

    if results:
        routing_logger.info(f"[{agent_name}] 文件更新: {'; '.join(results)}")


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------

async def run_agent(
    agent_name: str,
    latest_user_input: str,
    scene_summary: str = "",
    raw_messages: list[dict] | None = None,
) -> str:
    """运行指定角色的 Agent，返回清理后的响应文本。"""
    start = time.time()

    relevant = search_memories(agent_name, latest_user_input)
    memory_prefix = f"<relevant_memories>\n{relevant}\n</relevant_memories>" if relevant else ""
    full_input = _build_user_message(
        agent_name,
        latest_user_input,
        memory_prefix,
        raw_messages=raw_messages,
    )

    soul_content = read_agent_file(agent_name, "soul.md")
    system_prompt = _build_system_prompt(agent_name, soul_content)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": full_input},
    ]

    config = get_narrator_llm_config() if agent_name == "narrator" else get_llm_config()
    try:
        async with OpenAICompatibleClient(**config) as client:
            response = await asyncio.wait_for(
                client.chat(messages),
                timeout=AGENT_RUN_TIMEOUT_SECONDS,
            )
        routing_logger.info(f"{agent_name} 运行完成，耗时 {time.time() - start:.1f}秒")
        log_agent_call(agent_name, config["model"], messages, response)

        parsed = parse_agent_response(response["content"], agent_name)
        await _apply_response_updates(agent_name, parsed)
        return clean_response(parsed.content)

    except asyncio.TimeoutError:
        routing_logger.error(f"{agent_name} 运行超时（{time.time() - start:.1f}秒），强制终止")
        return f"[{agent_name} 回应超时，请稍后再试]"


# ---------------------------------------------------------------------------
# 多 Agent 编排
# ---------------------------------------------------------------------------


def _parse_choices(response_text: str) -> list[str]:
    """从模型输出中提取 <choices> 标签内的选项列表。"""
    match = re.search(r"<choices>(.*?)</choices>", response_text, re.DOTALL)
    if not match:
        return []
    lines = [line.strip() for line in match.group(1).strip().split("\n") if line.strip()]
    return lines[:3]


async def generate_choices(scene_description: str, agent_responses: list[tuple[str, str]]) -> list[str]:
    """根据当前场景和角色回应生成玩家可选行动。

    Args:
        scene_description: 旁白的场景描述
        agent_responses: [(角色名, 回应内容), ...] 列表

    Returns:
        选项文本列表（2-3 个），失败返回空列表
    """
    choices_prompt = (PROJECT_ROOT / "prompts" / "choices_prompt.txt").read_text(encoding="utf-8")
    raw_messages = load_conversation_history(limit=None)
    parts: list[str] = []
    history = build_history_transcript("narrator", raw_messages)
    if history:
        parts.append(f"【近期对话】\n{history}")
    if scene_description:
        parts.append(f"【场景】\n{scene_description}")
    for name, response in agent_responses:
        parts.append(f"【{name}】\n{response}")
    messages = [
        {"role": "system", "content": choices_prompt},
        {"role": "user", "content": "\n\n".join(parts)},
    ]

    config = get_choices_llm_config()
    try:
        async with OpenAICompatibleClient(**config) as client:
            response = await asyncio.wait_for(
                client.chat(messages),
                timeout=30,
            )
        log_agent_call("choices", config["model"], messages, response)
        return _parse_choices(response["content"])
    except Exception as e:
        routing_logger.warning(f"选项生成失败: {e}")
        return []


def parse_narrator_response(content: str) -> tuple[list[str], str]:
    """解析 narrator 响应，提取 TARGETS 和场景描述。

    Returns:
        (targets, scene_description): 目标角色列表和场景描述文本
    """
    valid_agents = get_agent_names(include_narrator=False)

    targets_pattern = re.compile(r"TARGETS\s*:?\s*\[?([^\]\n]*)\]?", re.IGNORECASE)
    all_matches = list(targets_pattern.finditer(content))

    if not all_matches:
        return [], content

    targets_match = all_matches[-1]
    targets_str = targets_match.group(1)
    targets = [
        t.strip().lower()
        for t in targets_str.split(",")
        if t.strip() and t.strip().lower() in valid_agents
    ]

    scene_description = content[targets_match.end():].strip()

    # 防御：截断 narrator 输出中混入的角色台词
    for agent in valid_agents:
        soul_content = read_agent_file(agent, "soul.md")
        names_to_check = [agent]
        display_name = get_display_name(agent, soul_content) if soul_content else None
        if display_name and display_name != agent:
            names_to_check.append(display_name)

        for name in names_to_check:
            for pattern in [
                re.compile(rf"^{re.escape(name)}\s*[:：]", re.MULTILINE | re.IGNORECASE),
                re.compile(rf"^##\s*{re.escape(name)}", re.MULTILINE | re.IGNORECASE),
            ]:
                m = pattern.search(scene_description)
                if m:
                    routing_logger.warning(
                        f"[narrator] 场景描述中检测到角色台词 '{name}'，已截断"
                    )
                    scene_description = scene_description[: m.start()].strip()
                    break

    return targets, scene_description


async def call_narrator_and_route(user_input: str) -> tuple[list[str], str, bool]:
    """调用 narrator 获取路由决策和场景描述

    Args:
        user_input: 玩家输入

    Returns:
        (targets, scene_description, is_valid): 目标角色列表、场景描述、是否有效
    """
    raw_messages = load_conversation_history(limit=None)

    narrator_content = await run_agent("narrator", user_input, raw_messages=raw_messages)
    targets, scene_description = parse_narrator_response(narrator_content)

    # TARGETS 缺失时重试一次
    if not targets:
        routing_logger.warning("narrator 响应缺少 TARGETS，重试中...")
        narrator_content = await run_agent("narrator", user_input, raw_messages=raw_messages)
        targets, scene_description = parse_narrator_response(narrator_content)
        if not targets:
            routing_logger.warning("narrator 重试后仍缺少 TARGETS")

    is_valid = is_valid_response(narrator_content, "narrator")

    routing_logger.info(f"narrator 决定 targets: {targets}")
    return targets, scene_description, is_valid


async def run_agent_in_scene(
    agent_name: str,
    targets: list[str],
    user_input: str,
    scene_summary: str = "",
) -> str | None:
    """在场景上下文中运行单个角色并广播响应

    处理历史加载、agent 调用、后处理、广播。不包含 UI 展示。

    Args:
        agent_name: 角色名
        targets: 当前回合所有目标角色
        user_input: 玩家输入
        scene_summary: 旁白的场景描述

    Returns:
        处理后的响应文本，失败返回 None
    """
    from engine.message_router import message_router

    raw_messages = load_conversation_history(limit=None)
    response = await run_agent(
        agent_name,
        user_input,
        scene_summary=scene_summary,
        raw_messages=raw_messages,
    )
    response = process_character_response(response)
    is_valid = is_valid_response(response, agent_name)

    # 只有有效响应才广播到 jsonl（让后续角色能看到）
    if is_valid:
        await message_router.broadcast_agent_response(
            agent_name, targets, response
        )

    return response
