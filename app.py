"""Chainlit 入口"""

import asyncio
import json
import os
import re
from datetime import datetime

import chainlit as cl
from dotenv import load_dotenv

from core.agent_runner import agent_manager, broadcaster
from core.memory_consolidator import CONSOLIDATION_INTERVAL, memory_consolidator
from core.routing_logger import routing_logger

# 加载环境变量
load_dotenv()

# 对话轮次计数器（每个 session 独立）
_message_counter: int = 0

# 对话历史条数配置（从环境变量读取）
HISTORY_LIMIT_NARRATOR = int(os.getenv("HISTORY_LIMIT_NARRATOR", "20"))
HISTORY_LIMIT_DEFAULT = int(os.getenv("HISTORY_LIMIT_DEFAULT", "10"))


def clean_response(content: str) -> str:
    """清理回复内容，移除 thinking 部分"""
    if not content:
        return content

    # 移除 <thinking>...</thinking> 标签及其内容
    content = re.sub(r"<thinking>.*?</thinking>", "", content, flags=re.DOTALL)

    # 移除可能的 think 标签变体
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)

    # 移除可能的 reasoning 标签
    content = re.sub(r"<reasoning>.*?</reasoning>", "", content, flags=re.DOTALL)

    # 清理多余的空行
    content = re.sub(r"\n{3,}", "\n\n", content)

    return content.strip()


def limit_actions(content: str, max_actions: int = 3) -> str:
    """限制角色回复中（）动作描写的数量，只保留前 max_actions 个。

    仅处理全角括号（）的动作描写，超出上限的直接删除。
    """
    if not content:
        return content

    action_pattern = re.compile(r"（[^）]*）")
    actions_found = 0

    def _replace(match: re.Match) -> str:
        nonlocal actions_found
        actions_found += 1
        if actions_found <= max_actions:
            return match.group(0)
        return ""

    result = action_pattern.sub(_replace, content)

    # 清理删除动作后可能残留的多余空格和空行
    result = re.sub(r"  +", " ", result)
    result = re.sub(r"\n{3,}", "\n\n", result)

    return result.strip()


def limit_ellipsis(content: str, max_ellipsis: int = 3) -> str:
    """限制回复中省略号的数量，只保留前 max_ellipsis 个。

    匹配中文省略号（……）和连续句点（...、..），超出上限的直接删除。
    """
    if not content:
        return content

    ellipsis_pattern = re.compile(r"……|\.{2,}")
    count = 0

    def _replace(match: re.Match) -> str:
        nonlocal count
        count += 1
        if count <= max_ellipsis:
            return match.group(0)
        return ""

    result = ellipsis_pattern.sub(_replace, content)

    # 清理残留的多余空格
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

    all_agents = ["lilith", "mitsuki", "narrator"]
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
    all_agents = ["lilith", "mitsuki", "narrator"]

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
    opening_message = {
        "timestamp": timestamp,
        "role": "narrator",
        "content": default_opening,
        "visible_to": ["lilith", "mitsuki", "narrator"],
    }

    raw_path = f"agents/narrator/memory/raw/{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    os.makedirs(os.path.dirname(raw_path), exist_ok=True)
    with open(raw_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(opening_message, ensure_ascii=False) + "\n")

    return default_opening if show_opening else ""


@cl.on_chat_start
async def on_chat_start():
    """聊天开始时的初始化 - 有记忆直接加载"""

    all_agents = ["lilith", "mitsuki", "narrator"]

    # 检查是否有存档
    has_save = has_existing_save()

    if not has_save:
        # 新游戏，重置记忆并发送开场
        await cl.Message(content="已重置记忆，开始新游戏。").send()
        default_opening = await reset_game(show_opening=True)
        await cl.Message(content=default_opening, author="Narrator").send()
    else:
        # 从 narrator 的历史中还原最近消息
        recent_messages = load_recent_raw_messages(limit=5)
        if recent_messages:
            await cl.Message(content="继续上次游戏，以下是最近的对话回顾：").send()
            for msg in recent_messages:
                role = msg.get("role", "unknown")
                content = msg.get("content", "").strip()
                if not content:
                    continue
                if role == "player":
                    # 玩家消息用 User author 展示
                    await cl.Message(content=content, author="User").send()
                elif role == "narrator":
                    await cl.Message(content=content, author="Narrator").send()
                else:
                    await cl.Message(content=content, author=role.capitalize()).send()
        else:
            await cl.Message(content="继续上次游戏。").send()


@cl.on_message
async def on_message(message: cl.Message):
    """处理用户消息"""
    global _message_counter
    user_input = message.content

    # 处理重置命令
    if user_input.strip() == "/reset":
        await cl.Message(content="✅ 游戏已重置，开始新故事...").send()
        default_opening = await reset_game(show_opening=True)
        await cl.Message(content=default_opening, author="Narrator").send()
        _message_counter = 0
        return

    _message_counter += 1

    routing_logger.info(f"玩家输入: {user_input}")
    # 1. 先调用 narrator（导演）决定场景和 targets
    # narrator 需要更多历史来理解全局上下文
    narrator_history = broadcaster.load_recent_history(
        "narrator", limit=HISTORY_LIMIT_NARRATOR
    )
    if narrator_history:
        narrator_input = (
            f"最近对话历史:\n\n{narrator_history}\n\n---\n\n玩家新消息: {user_input}"
        )
    else:
        narrator_input = user_input

    narrator_response = await agent_manager.run_agent("narrator", narrator_input)
    narrator_content = clean_response(narrator_response)

    # 解析 narrator 的 TARGETS 指令
    targets = []
    scene_description = narrator_content

    # 查找最后一个 TARGETS: [...] 或 **TARGETS:** [...] 格式
    # LLM 在 tool 调用前后可能都输出文本，导致多段重复；取最后一个匹配确保拿到最终版本
    targets_pattern = re.compile(
        r"\*{0,2}TARGETS\*{0,2}:?\s*\[([^\]]*)\]", re.IGNORECASE
    )
    all_matches = list(targets_pattern.finditer(narrator_content))
    if all_matches:
        targets_match = all_matches[-1]  # 取最后一个匹配
        targets_str = targets_match.group(1)
        # 解析角色列表
        targets = [t.strip().lower() for t in targets_str.split(",") if t.strip()]
        # 只取最后一个 TARGETS 行之后的文本，丢弃前面的重复内容
        scene_description = narrator_content[targets_match.end() :].strip()

    # 过滤有效角色
    valid_agents = ["lilith", "mitsuki"]
    targets = [t for t in targets if t in valid_agents]

    routing_logger.info(f"narrator 决定 targets: {targets}")

    # 2. 广播玩家消息到所有 targets + narrator（让角色们能看到玩家消息）
    await broadcaster.broadcast_player_message(targets, user_input)

    # 3. 如果有场景描述，广播给所有 targets（让角色们能看到旁白）
    if scene_description:
        await broadcaster.broadcast_agent_response(
            "narrator", targets, scene_description
        )
        await cl.Message(content=scene_description, author="Narrator").send()

    # 4. 如果没有角色需要回应，结束
    if not targets:
        print("[导演] 无角色需要回应")
        return

    # 5. 顺序调用 targets 中的角色（让后面的角色能看到前面的回应）
    results = []
    for agent_name in targets:
        try:
            # 加载该角色的历史上下文（默认角色使用 HISTORY_LIMIT_DEFAULT）
            history = broadcaster.load_recent_history(
                agent_name, limit=HISTORY_LIMIT_DEFAULT
            )

            # 构建完整输入
            parts = []
            if history:
                parts.append(f"最近对话历史:\n\n{history}")
            parts.append(f"玩家新消息: {user_input}")
            full_input = "\n\n---\n\n".join(parts)

            # 调用 agent
            response = await agent_manager.run_agent(agent_name, full_input)

            # 后处理：清理 thinking 标签 + 限制动作数量 + 限制省略号
            response = clean_response(response)
            response = limit_actions(response)
            response = limit_ellipsis(response)

            # 立即广播该角色的回应（已裁剪），让下一个角色能看到
            await broadcaster.broadcast_agent_response(agent_name, targets, response)

            results.append((agent_name, response))

        except Exception as e:
            print(f"Agent {agent_name} 运行失败: {e}")
            results.append((agent_name, f"[错误: {str(e)}]"))

    # 7. 展示给玩家（已在步骤 5 中完成后处理）
    for agent_name, response in results:
        if response:
            await cl.Message(
                content=response,
                author=agent_name.capitalize(),
            ).send()

    # 8. 每 N 轮触发记忆整理（后台执行，不阻塞用户交互）
    print(
        f"[调试] 当前轮次: {_message_counter}, CONSOLIDATION_INTERVAL: {CONSOLIDATION_INTERVAL}, 是否触发: {_message_counter % CONSOLIDATION_INTERVAL == 0}"
    )
    if _message_counter % CONSOLIDATION_INTERVAL == 0:
        all_agents = ["lilith", "mitsuki", "narrator"]
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
    print(f"可用角色: lilith (魅魔), mitsuki (青梅竹马), narrator (旁白)")
