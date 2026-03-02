"""Chainlit 入口"""

import asyncio
import os
import re

import chainlit as cl
from dotenv import load_dotenv

from engine.agent_manager import agent_manager
from engine.config import (
    get_agent_names,
    get_valid_response_agents,
    HISTORY_LIMIT_DEFAULT,
    HISTORY_LIMIT_NARRATOR,
)
from engine.message_router import message_router
from engine.text_utils import (
    clean_response,
    is_valid_response,
    process_character_response,
)
from game.save_manager import (
    export_save_archive,
    has_existing_save,
    import_save_archive,
    list_save_archives,
    load_conversation_history,
    reset_game,
)
from log_config.routing import routing_logger

from memory.consolidator import CONSOLIDATION_INTERVAL, memory_consolidator

# 加载环境变量
load_dotenv()


def _prepare_chainlit_database_url() -> None:
    """兼容 asyncpg：将 postgresql+asyncpg:// 规范化为 postgresql://。"""
    chainlit_db_url = os.getenv("CHAINLIT_DATABASE_URL", "").strip()
    db_url = chainlit_db_url or os.getenv("DATABASE_URL", "").strip()
    if db_url.startswith("postgresql+asyncpg://"):
        db_url = db_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    if db_url:
        os.environ["DATABASE_URL"] = db_url


_prepare_chainlit_database_url()


# =============================================================================
# 对话历史辅助函数
# =============================================================================


def _format_conversation_history(messages: list, agent_name: str, limit: int = 10) -> str:
    """格式化对话历史为文本字符串

    策略：只保留最新的一条 narrator 发言（设置场景），其他 narrator 发言过滤掉。
    这样可以让角色看到更多的角色/玩家互动历史，而不是被旁白占用空间。

    Args:
        messages: 原始消息列表（来自 load_conversation_history）
        agent_name: 角色名，用于按 visible_to 过滤
        limit: 返回最近多少条

    Returns:
        格式化的对话历史文本，如果无消息返回空字符串
    """
    if not messages:
        return ""

    # 按 visible_to 过滤：narrator 看全部，其他角色只看自己可见的
    if agent_name != "narrator":
        messages = [msg for msg in messages if agent_name in msg.get("visible_to", [])]

    # 取最近 limit 条
    recent = messages[-limit:]

    # 分离 narrator 和其他发言
    narrator_messages = [msg for msg in recent if msg.get("role") == "narrator"]
    other_messages = [msg for msg in recent if msg.get("role") != "narrator"]

    # 只保留最新的一条 narrator 发言（如果有的话）
    final_messages = other_messages
    if narrator_messages:
        final_messages.append(narrator_messages[-1])

    # 按原始顺序排序（保持时间顺序）
    final_messages.sort(key=lambda m: recent.index(m))

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


# =============================================================================
# on_chat_start
# =============================================================================


STORY_OPTIONS = [
    cl.Action(name="school", payload={"story": "school"}, label="🏫 私立城川中学 · 青春校园"),
    cl.Action(name="ancient", payload={"story": "ancient"}, label="🏯 烟雨江湖 · 宋代武侠"),
    cl.Action(name="modern", payload={"story": "modern"}, label="🌆 不期而遇 · 现代都市"),
]


async def _ask_story_choice() -> str:
    """弹出故事选择，返回 story_id；超时或取消则默认 school"""
    res = await cl.AskActionMessage(
        content="请选择你想要进入的故事世界：",
        actions=STORY_OPTIONS,
        timeout=120,
    ).send()
    if res and res.get("payload"):
        return res["payload"].get("story", "school")
    return "school"


async def _handle_new_game(story_id: str) -> tuple[str, str]:
    """处理新游戏启动：重置记忆并返回开场白（玩法介绍, 故事开始）"""
    await cl.Message(content="已重置记忆，开始新游戏。").send()
    return await reset_game(story_id)


async def _handle_continue_game() -> None:
    """处理续档：加载并显示最近对话历史"""
    recent_messages = load_conversation_history(limit=5)

    if not recent_messages:
        await cl.Message(content="继续上次游戏。").send()
        return

    await cl.Message(content="继续上次游戏，以下是最近的对话回顾：").send()

    for msg in recent_messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "").strip()
        if not content:
            continue

        author_map = {
            "player": "User",
            "narrator": "Narrator",
        }
        author = author_map.get(role, role.capitalize())
        await cl.Message(content=content, author=author).send()


@cl.on_chat_start
async def on_chat_start():
    """聊天开始时的初始化 - 有记忆直接加载"""
    has_save = has_existing_save()

    if not has_save:
        story_id = await _ask_story_choice()
        intro_text, opening_text = await _handle_new_game(story_id)
        # 发送玩法介绍
        if intro_text:
            await cl.Message(content=intro_text, author="Narrator").send()
        # 发送故事开场
        if opening_text:
            await cl.Message(content=opening_text, author="Narrator").send()
    else:
        await _handle_continue_game()


# =============================================================================
# on_message 辅助函数
# =============================================================================


def _parse_narrator_response(content: str) -> tuple[list[str], str]:
    """解析 narrator 响应，提取 TARGETS 和场景描述"""
    valid_agents = get_valid_response_agents()

    targets_pattern = re.compile(
        r"TARGETS\s*:?\s*\[?([^\]\n]*)\]?", re.IGNORECASE
    )
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

    scene_description = content[targets_match.end() :].strip()
    return targets, scene_description


async def _call_narrator_and_route(user_input: str) -> tuple[list[str], str, bool]:
    """调用 narrator 获取路由决策和场景描述"""
    raw_messages = load_conversation_history(limit=HISTORY_LIMIT_NARRATOR)
    narrator_history = _format_conversation_history(raw_messages, "narrator", limit=HISTORY_LIMIT_NARRATOR)
    narrator_input = (
        f"最近对话历史:\n\n{narrator_history}\n\n---\n\n玩家新消息: {user_input}"
        if narrator_history
        else user_input
    )

    narrator_response = await agent_manager.run_agent("narrator", narrator_input)
    narrator_content = clean_response(narrator_response)

    targets, scene_description = _parse_narrator_response(narrator_content)
    is_valid = is_valid_response(narrator_content, "narrator")

    routing_logger.info(f"narrator 决定 targets: {targets}")
    return targets, scene_description, is_valid


def _build_agent_input(history: str, user_input: str) -> str:
    """构建 agent 的完整输入"""
    parts = []
    if history:
        parts.append(f"最近对话历史:\n\n{history}")
    parts.append(f"玩家新消息: {user_input}")
    return "\n\n---\n\n".join(parts)


async def _process_target_agents(
    targets: list[str],
    user_input: str,
) -> list[tuple[str, str, bool]]:
    """顺序处理目标角色，收集响应"""
    results = []

    for agent_name in targets:
        try:
            raw_messages = load_conversation_history(limit=HISTORY_LIMIT_DEFAULT)
            history = _format_conversation_history(raw_messages, agent_name, limit=HISTORY_LIMIT_DEFAULT)
            full_input = _build_agent_input(history, user_input)

            response = await agent_manager.run_agent(agent_name, full_input)

            # 后处理：清理 thinking 标签 + 限制动作数量 + 限制省略号
            response = process_character_response(response)

            is_valid = is_valid_response(response, agent_name)

            # 只有有效响应才广播到 jsonl（让后续角色能看到）
            if is_valid:
                await message_router.broadcast_agent_response(
                    agent_name, targets, response
                )

            results.append((agent_name, response, is_valid))

        except Exception as e:
            print(f"Agent {agent_name} 运行失败: {e}")
            error_msg = f"[错误: {str(e)}]"
            results.append((agent_name, error_msg, False))

    return results



# =============================================================================
# 命令处理
# =============================================================================


async def _handle_save_command() -> bool:
    """处理 /save 命令"""
    save_path = await export_save_archive()

    if save_path:
        await cl.Message(
            content=f"✅ 存档已导出: `{save_path}`\n\n包含所有角色的记忆、对话历史和状态。",
        ).send()
    else:
        await cl.Message(
            content="❌ 存档导出失败，请检查日志。",
        ).send()

    return True


async def _handle_reset_command() -> bool:
    """处理 /reset 命令"""
    story_id = await _ask_story_choice()
    await cl.Message(content="✅ 游戏已重置，开始新故事...").send()
    intro_text, opening_text = await reset_game(story_id)
    # 发送玩法介绍
    if intro_text:
        await cl.Message(content=intro_text, author="Narrator").send()
    # 发送故事开场
    if opening_text:
        await cl.Message(content=opening_text, author="Narrator").send()
    cl.user_session.set("message_counter", 0)
    return True


async def _handle_load_command(user_input: str) -> bool:
    """处理 /load list 和 /load <序号> 命令"""
    parts = user_input.strip().split()

    if len(parts) < 2:
        await cl.Message(content="用法：`/load list` 查看存档，`/load <序号>` 加载存档").send()
        return True

    sub = parts[1].lower()

    # /load list：展示存档列表
    if sub == "list":
        saves = list_save_archives()
        if not saves:
            await cl.Message(content="暂无存档。先用 `/save` 保存一个吧。").send()
            return True
        lines = ["📂 **存档列表：**"]
        for i, s in enumerate(saves, start=1):
            focus_label = f"  · {s['focus']}" if s.get("focus") else ""
            lines.append(f"{i}. {s['display_time']}{focus_label}")
        lines.append("\n使用 `/load <序号>` 加载对应存档。")
        await cl.Message(content="\n".join(lines)).send()
        return True

    # /load <n>：按序号加载
    if not sub.isdigit():
        await cl.Message(content="用法：`/load list` 查看存档，`/load <序号>` 加载存档").send()
        return True

    index = int(sub)
    saves = list_save_archives()
    if index < 1 or index > len(saves):
        await cl.Message(content=f"序号 {index} 不存在，请先用 `/load list` 查看。").send()
        return True

    target = saves[index - 1]
    await cl.Message(content=f"⏳ 正在读取存档：{target['display_time']}…").send()

    success = await import_save_archive(target["filename"])
    if success:
        await cl.Message(content=f"✅ 读档成功：{target['display_time']}").send()
        cl.user_session.set("message_counter", 0)
        await _handle_continue_game()
    else:
        await cl.Message(content="❌ 读档失败，请检查日志。").send()

    return True


# 命令处理器映射表（精确匹配）
_COMMAND_HANDLERS: dict[str, callable] = {
    "/save": _handle_save_command,
    "/reset": _handle_reset_command,
}


# =============================================================================
# on_message
# =============================================================================


@cl.on_message
async def on_message(message: cl.Message):
    """处理用户消息"""
    user_input = message.content.strip()

    # 获取当前计数器（从 session 中）
    message_counter = cl.user_session.get("message_counter", 0)

    # 命令分发处理：前缀命令（/load 需要子参数）
    if user_input.lower().startswith("/load"):
        await _handle_load_command(user_input)
        return

    # 精确匹配命令（/save、/reset）
    handler = _COMMAND_HANDLERS.get(user_input)
    if handler:
        await handler()
        return

    message_counter += 1
    cl.user_session.set("message_counter", message_counter)

    routing_logger.info(f"玩家输入: {user_input}")

    # 1. 调用 narrator 获取路由决策和场景描述
    targets, scene_description, is_narrator_valid = await _call_narrator_and_route(
        user_input
    )

    # 2. 广播玩家消息到所有 targets
    await message_router.broadcast_player_message(targets, user_input)

    # 3. 广播场景描述并显示
    if scene_description and is_narrator_valid:
        await message_router.broadcast_agent_response(
            "narrator", targets, scene_description
        )
        await cl.Message(content=scene_description, author="Narrator").send()

    # 4. 如果没有角色需要回应，结束
    if not targets:
        print("[导演] 无角色需要回应")
        return

    # 5. 顺序处理目标角色
    results = await _process_target_agents(targets, user_input)

    # 6. 展示角色响应给玩家
    for agent_name, response, _ in results:
        if response:
            await cl.Message(
                content=response,
                author=agent_name.capitalize(),
            ).send()

    # 7. 每 N 轮触发记忆整理（后台执行，不阻塞用户交互）
    print(
        f"[调试] 当前轮次: {message_counter}, CONSOLIDATION_INTERVAL: {CONSOLIDATION_INTERVAL}, 是否触发: {message_counter % CONSOLIDATION_INTERVAL == 0}"
    )
    if message_counter % CONSOLIDATION_INTERVAL == 0:
        all_agents = get_agent_names()
        print(f"[调试] 触发记忆整理，目标角色: {all_agents}")
        asyncio.create_task(memory_consolidator.consolidate_all(all_agents))


@cl.on_chat_end
async def on_chat_end():
    """聊天结束时的清理"""
    await memory_consolidator.close()


if __name__ == "__main__":
    # 本地运行调试
    from datetime import datetime

    print("MemoBot 启动...")
    print(f"时间: {datetime.now().isoformat()}")
    agents = get_agent_names()
    print(f"可用角色: {', '.join(agents)}")
