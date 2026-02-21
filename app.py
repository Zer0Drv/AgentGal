"""Chainlit 入口"""

import asyncio
import json
import os
import re
from datetime import datetime

import chainlit as cl
from dotenv import load_dotenv

from core.agent_runner import agent_manager, broadcaster
from core.config import (
    get_agent_names,
    get_valid_response_agents,
    HISTORY_LIMIT_DEFAULT,
    HISTORY_LIMIT_NARRATOR,
    MAX_ACTIONS,
    MAX_ELLIPSIS,
)
from core.memory_consolidator import CONSOLIDATION_INTERVAL, memory_consolidator
from core.routing_logger import routing_logger

# 加载环境变量
load_dotenv()


def _clean_response(content: str) -> str:
    """清理回复内容，移除 thinking 标签"""
    if not content:
        return content
    content = re.sub(r"<thinking>.*?</thinking>|<think>.*?</think>|<reasoning>.*?</reasoning>", "", content, flags=re.DOTALL)
    content = re.sub(r"\n{3,}", "\n\n", content)
    content = re.sub(r"  +", " ", content)
    return content.strip()


def _process_character_response(content: str) -> str:
    """处理角色回复：清理 thinking + 限制动作和省略号"""
    if not content:
        return content

    # 清理 thinking
    result = re.sub(r"<thinking>.*?</thinking>|<think>.*?</think>|<reasoning>.*?</reasoning>", "", content, flags=re.DOTALL)

    # 限制动作描写（保留前 MAX_ACTIONS 个）
    action_count = 0
    def replace_action(m):
        nonlocal action_count
        action_count += 1
        return m.group(0) if action_count <= MAX_ACTIONS else ""
    result = re.sub(r"（[^）]*）", replace_action, result)

    # 限制省略号（保留前 MAX_ELLIPSIS 个）
    ellipsis_count = 0
    def replace_ellipsis(m):
        nonlocal ellipsis_count
        ellipsis_count += 1
        return m.group(0) if ellipsis_count <= MAX_ELLIPSIS else ""
    result = re.sub(r"……|\.{2,}", replace_ellipsis, result)

    # 清理空白
    result = re.sub(r"\n{3,}", "\n\n", result)
    result = re.sub(r"  +", " ", result)
    return result.strip()


def load_recent_raw_messages(limit: int = 10) -> list:
    """从 narrator 的 raw/ 目录加载最近的原始消息（narrator 拥有上帝视角，包含所有消息）"""
    import glob

    raw_dir = "agents/narrator/memory/raw"
    if not os.path.exists(raw_dir):
        return []

    # 按日期排序所有 jsonl 文件
    jsonl_files = sorted(glob.glob(f"{raw_dir}/*.jsonl"))
    if not jsonl_files:
        return []

    # 从所有文件中收集消息
    all_messages = []
    for filepath in jsonl_files:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        all_messages.append(json.loads(line.strip()))
                    except json.JSONDecodeError:
                        continue

    # 返回最后 limit 条
    return all_messages[-limit:]


def has_existing_save() -> bool:
    """检查是否有已存在的存档（任一角色有 jsonl 文件）"""
    import glob

    all_agents = get_agent_names()
    for agent_name in all_agents:
        raw_dir = f"agents/{agent_name}/memory/raw"
        if os.path.exists(raw_dir):
            jsonl_files = glob.glob(f"{raw_dir}/*.jsonl")
            if jsonl_files:
                return True
    return False


def reset_agent_memory(agent_name: str):
    """重置指定角色的所有记忆文件（保留 soul.md）"""
    import glob
    import shutil

    agent_path = f"agents/{agent_name}"
    example_path = f"examples/{agent_name}"

    # 1. 删除 raw/ 目录下的所有 jsonl 文件（对话历史）
    raw_dir = f"{agent_path}/memory/raw"
    if os.path.exists(raw_dir):
        for jsonl_file in glob.glob(f"{raw_dir}/*.jsonl"):
            try:
                os.remove(jsonl_file)
                print(f"  已删除: {os.path.basename(jsonl_file)}")
            except Exception as e:
                print(f"  删除失败 {jsonl_file}: {e}")

    # 2. 清空 memory.md（长期记忆）
    memory_path = f"{agent_path}/memory/memory.md"
    if os.path.exists(memory_path):
        try:
            with open(memory_path, "w", encoding="utf-8") as f:
                f.write("")
            print(f"  已清空: memory.md")
        except Exception as e:
            print(f"  清空失败 memory.md: {e}")

    # 3. 从 examples 恢复初始状态文件
    for filename in ["status.md", "user.md"]:
        example_file = f"{example_path}/{filename}"
        target_file = f"{agent_path}/{filename}"
        if os.path.exists(example_file):
            try:
                shutil.copy2(example_file, target_file)
                print(f"  已恢复: {filename}")
            except Exception as e:
                print(f"  恢复失败 {filename}: {e}")


def reset_logs():
    """重置日志文件"""
    log_files = [
        "logs/agent_calls_readable.log",
        "logs/agent_calls.jsonl",
        "logs/routing.log",
    ]
    for log_file in log_files:
        if os.path.exists(log_file):
            open(log_file, "w").close()


async def reset_game(show_opening: bool = True) -> str:
    """重置游戏，清空所有记忆并可选发送开场

    Returns:
        开场白内容（如果 show_opening=True）或空字符串
    """
    all_agents = get_agent_names()

    print(f"\n{'=' * 40}")
    print("重置游戏...")
    print(f"{'=' * 40}\n")

    # 重置所有角色记忆
    for agent_name in all_agents:
        print(f"[{agent_name}]")
        reset_agent_memory(agent_name)

    # 重置日志
    print("[日志]")
    reset_logs()

    print(f"\n{'=' * 40}")
    print("重置完成")
    print(f"{'=' * 40}\n")

    default_opening = """**私立桜庭学园 · 4月的清晨**

樱花瓣随风飘进教室的窗户。
你懒懒地坐在靠窗的座位上，看着窗外熟悉的景色。
新学期刚开始，距离毕业还有三个月。

讲台上，班主任拍了拍手：
"今天有位转学生加入我们班级。"

一个引人注目的身影走进教室。
"""

    # 将开场旁白写入所有角色的历史
    timestamp = datetime.now().isoformat()
    visible_agents = [a for a in all_agents if a != "narrator"]
    opening_message = {
        "timestamp": timestamp,
        "role": "narrator",
        "content": default_opening,
        "visible_to": visible_agents + ["narrator"],
    }

    raw_path = f"agents/narrator/memory/raw/{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    os.makedirs(os.path.dirname(raw_path), exist_ok=True)
    with open(raw_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(opening_message, ensure_ascii=False) + "\n")

    return default_opening if show_opening else ""


# =============================================================================
# on_chat_start 拆分
# =============================================================================

async def _handle_new_game() -> str:
    """
    处理新游戏启动：重置记忆并返回开场白

    Returns:
        开场白内容
    """
    await cl.Message(content="已重置记忆，开始新游戏。").send()
    return await reset_game(show_opening=True)


async def _handle_continue_game() -> None:
    """
    处理续档：加载并显示最近对话历史
    """
    recent_messages = load_recent_raw_messages(limit=5)

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
    # 检查是否有存档
    has_save = has_existing_save()

    if not has_save:
        # 新游戏，重置记忆并发送开场
        default_opening = await _handle_new_game()
        await cl.Message(content=default_opening, author="Narrator").send()
    else:
        # 续档，加载最近历史
        await _handle_continue_game()


# =============================================================================
# on_message 拆分
# =============================================================================

async def _handle_reset_command(user_input: str) -> bool:
    """
    处理 /reset 命令

    Args:
        user_input: 用户输入

    Returns:
        True 如果处理了命令，False 否则
    """
    if user_input.strip() != "/reset":
        return False

    await cl.Message(content="✅ 游戏已重置，开始新故事...").send()
    default_opening = await reset_game(show_opening=True)
    await cl.Message(content=default_opening, author="Narrator").send()
    cl.user_session.set("message_counter", 0)
    return True


def _parse_narrator_response(content: str) -> tuple[list[str], str]:
    """
    解析 narrator 响应，提取 TARGETS 和场景描述

    Args:
        content: narrator 原始响应

    Returns:
        (目标角色列表, 场景描述)
    """
    valid_agents = get_valid_response_agents()

    # 查找最后一个 TARGETS: [...] 格式
    targets_pattern = re.compile(r"\*{0,2}TARGETS\*{0,2}:?\s*\[([^\]]*)\]", re.IGNORECASE)
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
    return targets, scene_description


async def _call_narrator_and_route(user_input: str) -> tuple[list[str], str, bool]:
    """
    调用 narrator 获取路由决策和场景描述

    Args:
        user_input: 玩家输入

    Returns:
        (目标角色列表, 场景描述, narrator响应是否有效)
    """
    narrator_history = broadcaster.load_recent_history(
        "narrator", limit=HISTORY_LIMIT_NARRATOR
    )
    narrator_input = (
        f"最近对话历史:\n\n{narrator_history}\n\n---\n\n玩家新消息: {user_input}"
        if narrator_history else user_input
    )

    narrator_response = await agent_manager.run_agent("narrator", narrator_input)
    narrator_content = _clean_response(narrator_response)

    targets, scene_description = _parse_narrator_response(narrator_content)
    is_valid = not (
        narrator_content.startswith("[narrator 回应超时") or
        narrator_content.startswith("[错误:")
    )

    routing_logger.info(f"narrator 决定 targets: {targets}")
    return targets, scene_description, is_valid


def _build_agent_input(history: str, user_input: str) -> str:
    """构建 agent 的完整输入"""
    parts = []
    if history:
        parts.append(f"最近对话历史:\n\n{history}")
        # 检查历史中是否已经包含本轮玩家消息（避免重复）
        if f"玩家: {user_input}" not in history:
            parts.append(f"玩家新消息: {user_input}")
    else:
        parts.append(f"玩家新消息: {user_input}")
    return "\n\n---\n\n".join(parts)


async def _process_target_agents(
    targets: list[str],
    user_input: str,
) -> list[tuple[str, str, bool]]:
    """
    顺序处理目标角色，收集响应

    Args:
        targets: 要处理的角色列表
        user_input: 玩家输入

    Returns:
        [(角色名, 响应内容, 是否有效), ...]
    """
    results = []

    for agent_name in targets:
        try:
            history = broadcaster.load_recent_history(
                agent_name, limit=HISTORY_LIMIT_DEFAULT
            )
            full_input = _build_agent_input(history, user_input)

            response = await agent_manager.run_agent(agent_name, full_input)

            # 后处理：清理 thinking 标签 + 限制动作数量 + 限制省略号
            response = _process_character_response(response)

            is_valid = not (
                response.startswith(f"[{agent_name} 回应超时") or
                response.startswith("[错误:")
            )

            # 只有有效响应才广播到 jsonl（让后续角色能看到）
            if is_valid:
                await broadcaster.broadcast_agent_response(agent_name, targets, response)

            results.append((agent_name, response, is_valid))

        except Exception as e:
            print(f"Agent {agent_name} 运行失败: {e}")
            error_msg = f"[错误: {str(e)}]"
            results.append((agent_name, error_msg, False))

    return results


@cl.on_message
async def on_message(message: cl.Message):
    """处理用户消息"""
    user_input = message.content

    # 获取当前计数器（从 session 中）
    message_counter = cl.user_session.get("message_counter", 0)

    # 处理重置命令
    if await _handle_reset_command(user_input):
        return

    message_counter += 1
    cl.user_session.set("message_counter", message_counter)

    routing_logger.info(f"玩家输入: {user_input}")

    # 1. 调用 narrator 获取路由决策和场景描述
    targets, scene_description, is_narrator_valid = await _call_narrator_and_route(user_input)

    # 2. 广播玩家消息到所有 targets
    await broadcaster.broadcast_player_message(targets, user_input)

    # 3. 广播场景描述并显示
    if scene_description and is_narrator_valid:
        await broadcaster.broadcast_agent_response(
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
    print("MemoBot 启动...")
    print(f"时间: {datetime.now().isoformat()}")
    agents = get_agent_names()
    print(f"可用角色: {', '.join(agents)}")
