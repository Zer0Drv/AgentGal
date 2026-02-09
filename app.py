"""Chainlit 入口"""

import os
import asyncio
import re
import json
from datetime import datetime
from dotenv import load_dotenv

import chainlit as cl

from core.agent_runner import agent_manager, broadcaster
from core.memory_consolidator import memory_consolidator, CONSOLIDATION_INTERVAL
from core.routing_logger import routing_logger

# 加载环境变量
load_dotenv()

# 对话轮次计数器（每个 session 独立）
_message_counter: int = 0


def clean_response(content: str) -> str:
    """清理回复内容，移除 thinking 部分"""
    if not content:
        return content

    # 移除 <thinking>...</thinking> 标签及其内容
    content = re.sub(r'<thinking>.*?</thinking>', '', content, flags=re.DOTALL)

    # 移除可能的 think 标签变体
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)

    # 移除可能的 reasoning 标签
    content = re.sub(r'<reasoning>.*?</reasoning>', '', content, flags=re.DOTALL)

    # 清理多余的空行
    content = re.sub(r'\n{3,}', '\n\n', content)

    return content.strip()


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

    agent_path = f"agents/{agent_name}"

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
        

@cl.on_chat_start
async def on_chat_start():
    """聊天开始时的初始化 - 询问是否重置记忆"""

    all_agents = ["lilith", "mitsuki", "narrator"]

    # 检查是否有存档
    has_save = has_existing_save()

    should_reset = True
    if has_save:
        # 有存档，询问玩家是否重置
        res = await cl.AskActionMessage(
            content="检测到已有游戏存档，是否重置记忆开始新游戏？",
            actions=[
                cl.Action(name="reset", label="重置并新游戏", payload={"value": "reset"}),
                cl.Action(name="continue", label="继续上次游戏", payload={"value": "continue"}),
            ],
        ).send()
        should_reset = res and res.get("payload", {}).get("value") == "reset"

    if should_reset:
        # 重置所有角色的记忆
        print(f"\n{'='*40}")
        print("新游戏开始，重置所有角色记忆...")
        print(f"{'='*40}\n")
        for agent_name in all_agents:
            print(f"[{agent_name}]")
            reset_agent_memory(agent_name)
        # 重置日志
        print("[日志]")
        reset_logs()
        print(f"\n{'='*40}")
        print("记忆重置完成")
        print(f"{'='*40}\n")
        await cl.Message(content="已重置记忆，开始新游戏。").send()

        default_opening = """**私立桜庭学园 · 4月的清晨**

樱花瓣随风飘进教室的窗户。
你懒懒地坐在靠窗的座位上，看着窗外熟悉的景色。
新学期刚开始，距离毕业还有三个月。

讲台上，班主任拍了拍手：
"今天有位转学生加入我们班级。"

一个引人注目的身影走进教室。
"""

        # 发送消息给玩家
        await cl.Message(content=default_opening, author="Narrator").send()

        # 将开场旁白写入所有角色的历史，让他们知道场景设定
        from datetime import datetime
        import json

        timestamp = datetime.now().isoformat()
        opening_message = {
            "timestamp": timestamp,
            "role": "narrator",
            "content": default_opening,
            "visible_to": ["lilith", "mitsuki", "narrator"],
        }

        # 写入所有角色的 jsonl
        for agent_name in all_agents:
            raw_path = f"agents/{agent_name}/memory/raw/{datetime.now().strftime('%Y-%m-%d')}.jsonl"
            os.makedirs(os.path.dirname(raw_path), exist_ok=True)
            with open(raw_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(opening_message, ensure_ascii=False) + "\n")
    else:
        await cl.Message(content="继续上次游戏。").send()


@cl.on_message
async def on_message(message: cl.Message):
    """处理用户消息"""
    global _message_counter
    _message_counter += 1
    user_input = message.content

    # 1. 先调用 narrator（导演）决定场景和 targets
    narrator_history = broadcaster.load_recent_history("narrator", limit=10)
    if narrator_history:
        narrator_input = f"最近对话历史:\n\n{narrator_history}\n\n---\n\n玩家新消息: {user_input}"
    else:
        narrator_input = user_input

    narrator_response = await agent_manager.run_agent("narrator", narrator_input)
    narrator_content = clean_response(narrator_response)

    # 解析 narrator 的 TARGETS 指令
    targets = []
    scene_description = narrator_content

    # 查找 TARGETS: [...] 或 TARGETS[...] 格式
    targets_match = re.search(r'TARGETS:?\s*\[([^\]]*)\]', narrator_content, re.IGNORECASE)
    if targets_match:
        targets_str = targets_match.group(1)
        # 解析角色列表
        targets = [t.strip().lower() for t in targets_str.split(',') if t.strip()]
        # 移除 TARGETS 行，保留场景描述
        scene_description = re.sub(r'TARGETS:?\s*\[[^\]]*\]\n?', '', narrator_content, flags=re.IGNORECASE).strip()

    # 过滤有效角色
    valid_agents = ["lilith", "mitsuki"]
    targets = [t for t in targets if t in valid_agents]

    routing_logger.info(f"narrator 决定 targets: {targets}")

    # 2. 广播玩家消息到所有 targets + narrator（让角色们能看到玩家消息）
    await broadcaster.broadcast_player_message(targets, user_input)

    # 3. 如果有场景描述，广播给所有 targets（让角色们能看到旁白）
    if scene_description:
        await broadcaster.broadcast_agent_response("narrator", targets, scene_description)
        await cl.Message(content=scene_description, author="Narrator").send()

    # 4. 如果没有角色需要回应，结束
    if not targets:
        print("[导演] 无角色需要回应")
        return

    # 5. 顺序调用 targets 中的角色（让后面的角色能看到前面的回应）
    results = []
    for agent_name in targets:
        try:
            # 加载该角色的历史上下文（包含之前角色的回应）
            history = broadcaster.load_recent_history(agent_name, limit=10)

            # 构建完整输入
            parts = []
            if history:
                parts.append(f"最近对话历史:\n\n{history}")
            parts.append(f"玩家新消息: {user_input}")
            full_input = "\n\n---\n\n".join(parts)

            # 调用 agent
            response = await agent_manager.run_agent(agent_name, full_input)

            # 立即广播该角色的回应，让下一个角色能看到
            await broadcaster.broadcast_agent_response(agent_name, targets, response)

            results.append((agent_name, response))

        except Exception as e:
            print(f"Agent {agent_name} 运行失败: {e}")
            results.append((agent_name, f"[错误: {str(e)}]"))

    # 7. 展示给玩家
    for agent_name, response in results:
        cleaned_response = clean_response(response)
        if cleaned_response:
            await cl.Message(
                content=cleaned_response,
                author=agent_name.capitalize(),
            ).send()

    # 8. 每 N 轮触发后台记忆整理（不阻塞用户）
    if _message_counter % CONSOLIDATION_INTERVAL == 0:
        all_active = list(set(targets + ["narrator"]))
        asyncio.create_task(memory_consolidator.consolidate_all(all_active))


@cl.on_chat_end
async def on_chat_end():
    """聊天结束时的清理"""
    await memory_consolidator.close()


if __name__ == "__main__":
    # 本地运行调试
    print("MemoBot 启动...")
    print(f"时间: {datetime.now().isoformat()}")
    print(f"可用角色: lilith (魅魔), mitsuki (青梅竹马), narrator (旁白)")
