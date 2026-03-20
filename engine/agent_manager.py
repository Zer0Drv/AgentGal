"""Agent 运行器 - 按需构建 system prompt 并调用 LLM"""

import asyncio
import os
import re
import time
from pathlib import Path

from engine.config import (
    AGENT_RUN_TIMEOUT_SECONDS,
    HISTORY_HIGH,
    HISTORY_LOW,
    PROJECT_ROOT,
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
    _read_title,
    add_pending_event,
    _update_section_file,
    get_allowed_fields,
    mark_event_triggered,
    read_agent_file,
    read_sidecar_json,
    write_sidecar_json,
)
from game.save_manager import load_conversation_history
from memory.retrieval import search_memories


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


def _build_system_prompt(agent_name: str, soul_content: str) -> str:
    """构建 system prompt（仅包含稳定的身份与规则部分）。"""
    prompt_name = "narrator_prompt.txt" if agent_name == "narrator" else "character_prompt.txt"
    prompt_template = (PROJECT_ROOT / "prompts" / prompt_name).read_text(encoding="utf-8")
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
        status_fields=status_fields,
        player_fields=player_fields,
        characters_scene_list=characters_scene_list,
        valid_targets=valid_targets,
    )


def _apply_high_low_watermark(
    agent_name: str,
    visible_indices: list[int],
    start_raw_index: int,
    high: int,
    low: int,
) -> tuple[int, list[int]]:
    """应用高低水位缓冲，返回本轮应显示的消息下标范围。

    Args:
        agent_name: 角色名，仅用于日志。
        visible_indices: 该 agent 可见的消息在 raw 列表中的绝对下标，已按时间升序排列。
        start_raw_index: 上轮持久化的窗口左边界（raw 绝对下标），用于跳过已截断的旧消息。
        high: 窗口上限；当前窗口消息数超过此值时触发截断。
        low: 截断目标；触发时保留最新的 low 条消息。

    Returns:
        (next_start_raw_index, kept_indices)：
        - next_start_raw_index：新的窗口左边界，调用方应持久化以供下轮使用。
        - kept_indices：本轮实际送入 context 的消息下标列表（raw 绝对下标子集）。
    """
    if not visible_indices:
        return 0, []

    # 用持久化的 start_raw_index 作为窗口左边界，过滤掉已被截断的旧消息
    kept_indices = [idx for idx in visible_indices if idx >= start_raw_index]
    if not kept_indices:
        # 窗口起点比所有可见消息都新（通常发生在重置/加载后），回退到全部可见
        start_raw_index = visible_indices[0]
        kept_indices = visible_indices

    if len(kept_indices) > high:
        # 触发高水位：一次性砍到 low 条，并把新的窗口起点持久化回去
        # 下次调用前不会再截断，只在尾部追加，直到再次超过 high
        routing_logger.info("[%s] 历史窗口触发高水位", agent_name)
        kept_indices = kept_indices[-low:]
        start_raw_index = kept_indices[0]

    return start_raw_index, kept_indices


def _get_windowed_visible_messages(agent_name: str, raw_messages: list[dict]) -> list[dict]:
    """按 agent 维度应用真正的高低水位窗口，并持久化窗口起点。"""
    if not raw_messages:
        return []

    # narrator 看全部消息；角色只看 visible_to 中包含自己的消息
    if agent_name != "narrator":
        visible_indices = [
            idx for idx, msg in enumerate(raw_messages) if agent_name in msg.get("visible_to", [])
        ]
    else:
        visible_indices = list(range(len(raw_messages)))

    if not visible_indices:
        return []

    # 从 sidecar 读取上次截断后的窗口起点（raw 消息列表中的绝对下标）
    _hw = read_sidecar_json(agent_name, ".history_window_state.json")
    start_raw_index = min(max(0, int(_hw.get("start_raw_index", 0))), len(raw_messages) - 1)
    next_start_raw_index, kept_indices = _apply_high_low_watermark(
        agent_name,
        visible_indices,
        start_raw_index,
        HISTORY_HIGH,
        HISTORY_LOW,
    )
    # 持久化新的窗口起点，供下次调用时直接跳过已截断的旧消息
    write_sidecar_json(agent_name, ".history_window_state.json", {"start_raw_index": max(0, next_start_raw_index)})
    return [raw_messages[idx] for idx in kept_indices]


def _build_history_transcript(
    agent_name: str,
    raw_messages: list[dict],
) -> str:
    """将 JSONL 原始消息转为单段历史文本。

    规则：
    - visible_to 过滤（narrator 看全部，角色只看自己可见的）
    - 真正的高低水位窗口：超过 HISTORY_HIGH 时砍到 HISTORY_LOW，之后只追加直到再次超限
    - 保留原始消息边界，不拆成多条 user/assistant message
    - 统一格式化为带说话者前缀的单段文本，利于放进一条大 user message
    """
    visible = _get_windowed_visible_messages(agent_name, raw_messages)

    if not visible:
        return ""

    lines: list[str] = []
    for msg in visible:
        role = msg.get("role", "unknown")
        content = re.sub(r"\n+", "\n", msg.get("content", "").strip())
        if not content:
            continue
        speaker = "玩家" if role == "player" else role
        lines.append(f"{speaker}: {content}")

    return "\n\n".join(lines)


def _build_user_message(
    agent_name: str,
    latest_user_input: str,
    memory_prefix: str,
    raw_messages: list[dict] | None = None,
) -> str:
    """构建单条大 user message，按稳定度排序上下文。

    parts 顺序：
    - `<growth>`
    - 最近对话历史
    - `<user_profile>`
    - `<status>`
    - `memory_prefix`（`<relevant_memories>`）
    - 本轮玩家输入

    narrator 不包含 `growth`、`user_profile` 和长期记忆召回块。
    """
    parts: list[str] = []
    user_content = ""

    growth_content: str = (read_agent_file(agent_name, "growth.md")) if agent_name != "narrator" else ""
    user_content: str = read_agent_file(agent_name, "user.md") if agent_name != "narrator" else ""
    history: str = _build_history_transcript(agent_name, raw_messages or [])
    status_content: str = read_agent_file(agent_name, "status.md")

    parts.append(f"<growth>\n{growth_content.strip()}\n</growth>" if growth_content else "")
    parts.append(f"最近对话历史:\n\n{history}" if history else "")
    parts.append(f"<user_profile>\n{user_content.strip()}\n</user_profile>" if user_content else '')
    parts.append(f"<status>\n{status_content}\n</status>" if status_content else '')  
    parts.append(memory_prefix if memory_prefix else '')
    parts.append(f"玩家新消息: {latest_user_input}")

    return "\n\n---\n\n".join(parts)


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

    try:
        existing = Path(memory_path).read_text(encoding="utf-8")
    except OSError:
        existing = ""
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
    return _update_section_file(user_path, field, content, allowed, _read_title(user_path, "# 玩家档案"), append=True)


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
        _safe_update("memory", lambda: _update_memory(agent_name, parsed.memory))

    if parsed.status:
        for field, content in parsed.status.items():
            _safe_update(f"status[{field}]", lambda f=field, c=content: _update_status(agent_name, f, str(c)))

    if parsed.player:
        for field, content in parsed.player.items():
            _safe_update(f"player[{field}]", lambda f=field, c=content: _update_player(agent_name, f, str(c)))

    # narrator 操作「待触发事件」，其他角色操作「打算」
    event_section = "待触发事件" if agent_name == "narrator" else "打算"

    if parsed.triggered:
        for event_name in parsed.triggered:
            _safe_update(f"triggered[{event_name}]", lambda n=event_name: mark_event_triggered(agent_name, n, event_section))

    if parsed.add_event:
        for event_desc in parsed.add_event:
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
    history = _build_history_transcript("narrator", raw_messages)
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
