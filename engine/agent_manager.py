"""Agent 运行器 - 按需构建 system prompt 并调用 LLM"""

import asyncio
import os
import re
import time
from pathlib import Path

from engine.config import (
    AGENT_RUN_TIMEOUT_SECONDS,
    HISTORY_LIMIT,
    PROJECT_ROOT,
    VECTOR_SEARCH_LIMIT,
    character_path,
    get_agent_names,
)
from engine.response_parser import parse_agent_response, parse_narrator_response
from engine.text_utils import clean_response, is_valid_response, process_character_response
from llm.llm_parser import OpenAICompatibleClient
from llm.providers import get_choices_llm_config, get_llm_config, get_narrator_llm_config
from log_config.agent_calls import log_agent_call
from log_config.routing import routing_logger
from memory.file_ops import (
    _append_section_file,
    _read_title,
    add_pending_event,
    _update_section_file,
    extract_status_field,
    get_allowed_fields,
    load_growth_for_prompt,
    mark_event_triggered,
    read_agent_file,
    read_file_tail,
)
from memory.vector_store import vector_store


# ---------------------------------------------------------------------------
# Agent 构建
# ---------------------------------------------------------------------------

def _get_display_name(agent_name: str, soul_content: str) -> str:
    """从 soul.md 内容提取中文显示名，回退到 agent_name。"""
    role_match = re.search(r"<role>\s*([^\n<]+)", soul_content)
    if role_match:
        name_match = re.match(r"([\u4e00-\u9fff·]+)", role_match.group(1).strip())
        if name_match:
            return name_match.group(1)
    title_match = re.search(r"^#\s+(.+)$", soul_content, re.MULTILINE)
    if title_match:
        return title_match.group(1).strip()
    return agent_name


def _load_prompt_template(agent_name: str) -> str:
    """加载 system prompt 模板文件。"""
    filename = "narrator_prompt.txt" if agent_name == "narrator" else "character_prompt.txt"
    return (PROJECT_ROOT / "prompts" / filename).read_text(encoding="utf-8")


def _build_system_prompt(agent_name: str, soul_content: str) -> str:
    """构建 system prompt（仅包含不变/极少变的身份与规则部分）。"""
    growth_content = load_growth_for_prompt(agent_name) if agent_name != "narrator" else ""
    prompt_template = _load_prompt_template(agent_name)
    # 「打算」由 <triggered>/<add_event> 专用标签管理，不暴露给 <status> 覆盖
    _status_excluded = {"打算"} if agent_name != "narrator" else set()
    status_fields = "、".join(f for f in get_allowed_fields(agent_name, "status") if f not in _status_excluded)
    player_fields = "、".join(get_allowed_fields(agent_name, "user")) if agent_name != "narrator" else ""
    display_name = _get_display_name(agent_name, soul_content)

    characters = get_agent_names(include_narrator=False)
    characters_scene_list = "\n".join(
        f"- {_get_display_name(c, read_agent_file(c, 'soul.md'))}：[位置] 或 不在场"
        for c in characters
    )
    valid_targets = ", ".join(characters)

    return prompt_template.format(
        agent_name=agent_name,
        display_name=display_name,
        soul=soul_content,
        growth=growth_content,
        status_fields=status_fields,
        player_fields=player_fields,
        characters_scene_list=characters_scene_list,
        valid_targets=valid_targets,
    )


# ---------------------------------------------------------------------------
# 记忆注入
# ---------------------------------------------------------------------------

def _extract_user_message(full_input: str) -> str:
    """从拼接输入中提取原始玩家消息，用于 RAG 搜索。"""
    match = re.search(r"玩家新消息:\s*(.+)$", full_input, re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else full_input.strip()


def _build_search_query(agent_name: str, user_input: str, scene_summary: str = "") -> str:
    """构建上下文感知的 RAG query。

    旁白：玩家原话 + 叙事焦点 + 场景
    角色：玩家原话 + 旁白场景摘要 + 心境 + 在意的事
    """
    parts = [user_input]
    status = read_agent_file(agent_name, "status.md")

    if agent_name == "narrator":
        focus = extract_status_field(status, "叙事焦点")
        scene = extract_status_field(status, "场景")
        if focus:
            parts.append(focus)
        if scene:
            parts.append(scene)
    else:
        if scene_summary:
            parts.append(scene_summary)
        mood = extract_status_field(status, "心境")
        concerns = extract_status_field(status, "在意的事")
        if mood:
            parts.append(mood)
        if concerns:
            parts.append(concerns)

    return " | ".join(p for p in parts if p)


def _search_memories(agent_name: str, query: str) -> str:
    """语义搜索向量库，返回格式化记忆字符串。"""
    limit = VECTOR_SEARCH_LIMIT
    results = vector_store.search(agent_name, query, limit=limit, kind="memory")
    memories = [r["content"].strip() for r in results if r["content"].strip()]
    return "\n\n---\n\n".join(memories) if memories else "（无相关记忆）"


def _build_memory_prefix(agent_name: str, user_input: str, scene_summary: str = "") -> str:
    """组装记忆上下文前缀。

    角色：只用 RAG 召回（避免 memory.md 尾部重复条目形成回声室）。
    旁白：RAG 召回 + 最近记忆（旁白需要连续性上下文把握节奏）。
    """
    query = _build_search_query(agent_name, user_input, scene_summary)
    relevant = _search_memories(agent_name, query)
    parts = [f"<relevant_memories>\n{relevant}\n</relevant_memories>"]

    if agent_name == "narrator":
        recent = read_file_tail(character_path(agent_name, "memory.md"), lines=5) or "（尚无记忆）"
        parts.append(f"<recent_memories>\n{recent}\n</recent_memories>")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 响应后处理：写回文件
# ---------------------------------------------------------------------------

def _update_memory(agent_name: str, memory_content: str) -> str:
    """追加 memory 内容到 memory.md（带去重）。"""
    if not memory_content or not memory_content.strip():
        return "内容为空，跳过"

    memory_path = character_path(agent_name, "memory.md")
    os.makedirs(os.path.dirname(memory_path), exist_ok=True)
    clean = memory_content.replace("\\n", "\n").strip()

    def _parse_entries(text: str) -> list[str]:
        entries, current = [], []
        for line in text.split("\n"):
            if line.strip().startswith("##") or (line.strip().startswith("-") and "**" in line):
                if current:
                    entries.append("\n".join(current).strip())
                current = [line]
            elif line.strip() or current:
                current.append(line)
        if current:
            entries.append("\n".join(current).strip())
        return entries

    existing = Path(memory_path).read_text(encoding="utf-8") if os.path.exists(memory_path) else ""
    existing_set = set(_parse_entries(existing))
    unique = [e for e in _parse_entries(clean) if e and e not in existing_set]

    if not unique:
        return "所有 entry 已存在，跳过"

    to_append = "\n\n".join(unique)
    if existing.strip():
        with open(memory_path, "a", encoding="utf-8") as f:
            f.write(f"\n\n{to_append}")
    else:
        with open(memory_path, "w", encoding="utf-8") as f:
            f.write(f"# {agent_name} 的长期记忆\n\n{to_append}")

    return f"已追加 {len(unique)} 个新 entry"


def _update_status(agent_name: str, field: str, content: str) -> str:
    """覆盖更新 status.md 的指定字段。"""
    # 事件队列只能通过 add_pending_event/mark_event_triggered 操作，禁止整体覆盖
    if agent_name != "narrator" and field == "打算":
        routing_logger.warning(f"[{agent_name}] 禁止通过 <status> 覆盖「打算」字段，请使用 <triggered>/<add_event>")
        return "禁止覆盖「打算」字段，请用 <triggered>/<add_event> 逐条管理"
    if agent_name == "narrator" and field == "待触发事件":
        routing_logger.warning("[narrator] 禁止通过 <status> 覆盖「待触发事件」字段，请使用 <triggered>/<add_event>")
        return "禁止覆盖「待触发事件」字段，请用 <triggered>/<add_event> 逐条管理"
    allowed = get_allowed_fields(agent_name, "status")
    if field not in allowed:
        routing_logger.warning(f"[{agent_name}] 不允许的 status 字段: {field}")
        return f"字段 {field} 不在白名单中"
    status_path = character_path(agent_name, "status.md")
    return _update_section_file(status_path, field, content, allowed, _read_title(status_path, "# 我的状态"))


def _update_player(agent_name: str, field: str, content: str) -> str:
    """追加更新 user.md 的指定字段。"""
    allowed = get_allowed_fields(agent_name, "user")
    if field not in allowed:
        routing_logger.warning(f"[{agent_name}] 不允许的 player 字段: {field}")
        return f"字段 {field} 不在白名单中"
    user_path = character_path(agent_name, "user.md")
    return _append_section_file(user_path, field, content, allowed, _read_title(user_path, "# 玩家档案"))


async def _apply_response_updates(agent_name: str, parsed) -> None:
    """将解析后的 XML 更新指令写回对应文件。"""
    results = []

    if parsed.memory:
        try:
            results.append(f"memory: {_update_memory(agent_name, parsed.memory)}")
        except Exception as e:
            routing_logger.error(f"[{agent_name}] 更新 memory 失败: {e}")

    if parsed.status:
        try:
            for field, content in parsed.status.items():
                results.append(f"status[{field}]: {_update_status(agent_name, field, str(content))}")
        except Exception as e:
            routing_logger.error(f"[{agent_name}] 更新 status 失败: {e}")

    if parsed.player:
        try:
            for field, content in parsed.player.items():
                results.append(f"player[{field}]: {_update_player(agent_name, field, str(content))}")
        except Exception as e:
            routing_logger.error(f"[{agent_name}] 更新 player 失败: {e}")


    # narrator 操作「待触发事件」，其他角色操作「打算」
    event_section = "待触发事件" if agent_name == "narrator" else "打算"

    if parsed.triggered:
        try:
            for event_name in parsed.triggered:
                results.append(f"triggered[{event_name}]: {mark_event_triggered(agent_name, event_name, event_section)}")
        except Exception as e:
            routing_logger.error(f"[{agent_name}] 标记触发事件失败: {e}")

    if parsed.add_event:
        try:
            for event_desc in parsed.add_event:
                results.append(f"add_event: {add_pending_event(agent_name, event_desc, event_section)}")
        except Exception as e:
            routing_logger.error(f"[{agent_name}] 插入新事件失败: {e}")

    if results:
        routing_logger.info(f"[{agent_name}] 文件更新: {'; '.join(results)}")


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------

async def run_agent(agent_name: str, user_input: str, scene_summary: str = "") -> str:
    """运行指定角色的 Agent，返回清理后的响应文本。"""
    start = time.time()

    pure_input = _extract_user_message(user_input)
    memory_prefix = _build_memory_prefix(agent_name, pure_input, scene_summary)

    # 动态上下文：当前时间 + status + user_profile + 记忆 + 用户输入
    context_parts: list[str] = []
    if agent_name != "narrator":
        narrator_status = read_agent_file("narrator", "status.md")
        current_time = extract_status_field(narrator_status, "当前时间") if narrator_status else ""
        if current_time:
            context_parts.append(f"<current_time>{current_time}</current_time>")
    status_content = read_agent_file(agent_name, "status.md")
    context_parts.append(f"<status>\n{status_content if status_content else '（尚无状态记录）'}\n</status>")
    if agent_name != "narrator":
        user_content = read_agent_file(agent_name, "user.md")
        context_parts.append(f"<user_profile>\n{user_content if user_content else '（尚无玩家认知）'}\n</user_profile>")
    if memory_prefix:
        context_parts.append(memory_prefix)
    context_parts.append(user_input)
    full_input = "\n\n---\n\n".join(context_parts)

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
# 对话历史构建
# ---------------------------------------------------------------------------



def format_conversation_history(messages: list, agent_name: str, limit: int = 10) -> str:
    """格式化对话历史为文本字符串

    策略：只保留最新的一条 narrator 发言（设置场景），其他 narrator 发言过滤掉。
    这样可以让角色看到更多的角色/玩家互动历史，而不是被旁白占用空间。

    调用方应传入足够多的原始消息（建议 limit * 5），以保证过滤后仍有足够的有效消息。

    Args:
        messages: 原始消息列表（来自 load_conversation_history，应传入足够多的消息）
        agent_name: 角色名，用于按 visible_to 过滤
        limit: 返回最近多少条有效消息（不包括被过滤的旧narrator）

    Returns:
        格式化的对话历史文本，如果无消息返回空字符串
    """
    if not messages:
        return ""

    # 按 visible_to 过滤：narrator 看全部，其他角色只看自己可见的
    if agent_name != "narrator":
        recent = [msg for msg in messages if agent_name in msg.get("visible_to", [])]
    else:
        recent = messages

    # 只保留最新一条 narrator 发言，腾出空间给更多角色/玩家互动
    narrator_messages = [msg for msg in recent if msg.get("role") == "narrator"]
    other_messages = [msg for msg in recent if msg.get("role") != "narrator"]
    final_messages = other_messages
    if narrator_messages:
        final_messages.append(narrator_messages[-1])
    final_messages.sort(key=lambda m: recent.index(m))

    # 取最近 limit 条有效消息
    final_messages = final_messages[-limit:]

    # 格式化为文本
    formatted = []
    for msg in final_messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "player":
            formatted.append(f"玩家: {content}")
        else:
            formatted.append(f"{role}: {content}")

    return "\n".join(formatted)


def _build_agent_input(history: str, user_input: str) -> str:
    """构建 agent 的完整输入"""
    parts = []
    if history:
        parts.append(f"最近对话历史:\n\n{history}")
    parts.append(f"玩家新消息: {user_input}")
    return "\n\n---\n\n".join(parts)


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
    from game.save_manager import load_conversation_history

    parts = []

    # 加载最近对话历史，复用现有格式化逻辑（narrator 视角跳过 visible_to 过滤，且只保留最新一条旁白）
    raw_messages = load_conversation_history(limit=30)
    history = format_conversation_history(raw_messages, "narrator", limit=6)
    if history:
        parts.append(f"【近期对话】\n{history}")

    if scene_description:
        parts.append(f"【场景】\n{scene_description}")
    for name, response in agent_responses:
        parts.append(f"【{name}】\n{response}")

    user_content = "\n\n".join(parts)
    choices_prompt = (PROJECT_ROOT / "prompts" / "choices_prompt.txt").read_text(encoding="utf-8")
    messages = [
        {"role": "system", "content": choices_prompt},
        {"role": "user", "content": user_content},
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


async def call_narrator_and_route(user_input: str) -> tuple[list[str], str, bool]:
    """调用 narrator 获取路由决策和场景描述

    Args:
        user_input: 玩家输入

    Returns:
        (targets, scene_description, is_valid): 目标角色列表、场景描述、是否有效
    """
    from game.save_manager import load_conversation_history

    raw_messages = load_conversation_history(limit=HISTORY_LIMIT * 5)
    narrator_history = format_conversation_history(raw_messages, "narrator", limit=HISTORY_LIMIT)
    narrator_input = (
        f"最近对话历史:\n\n{narrator_history}\n\n---\n\n玩家新消息: {user_input}"
        if narrator_history
        else user_input
    )

    narrator_response = await run_agent("narrator", narrator_input)
    narrator_content = clean_response(narrator_response)

    targets, scene_description = parse_narrator_response(narrator_content)

    # TARGETS 缺失时重试一次
    if not targets:
        routing_logger.warning("narrator 响应缺少 TARGETS，重试中...")
        narrator_response = await run_agent("narrator", narrator_input)
        narrator_content = clean_response(narrator_response)
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
    from game.save_manager import load_conversation_history

    raw_messages = load_conversation_history(limit=HISTORY_LIMIT * 5)
    history = format_conversation_history(raw_messages, agent_name, limit=HISTORY_LIMIT)
    full_input = _build_agent_input(history, user_input)

    response = await run_agent(agent_name, full_input, scene_summary=scene_summary)
    response = process_character_response(response)
    is_valid = is_valid_response(response, agent_name)

    # 只有有效响应才广播到 jsonl（让后续角色能看到）
    if is_valid:
        await message_router.broadcast_agent_response(
            agent_name, targets, response
        )

    return response
