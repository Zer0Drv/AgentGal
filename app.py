"""Chainlit 入口"""

import os
import asyncio
import re
import json
from datetime import datetime
from dotenv import load_dotenv

import chainlit as cl

from core.agent_runner import agent_manager, broadcaster

# 加载环境变量
load_dotenv()


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


def reset_agent_memory(agent_name: str):
    """重置指定角色的所有记忆文件（保留 soul.md，从模板恢复 tasks.md 和 user.md）"""
    import shutil
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

    # 3. 从模板恢复 tasks.md（当前目标）
    tasks_default = f"{agent_path}/tasks.default.md"
    tasks_path = f"{agent_path}/tasks.md"
    if os.path.exists(tasks_default):
        try:
            shutil.copy2(tasks_default, tasks_path)
            print(f"  已恢复: tasks.md")
        except Exception as e:
            print(f"  恢复失败 tasks.md: {e}")

    # 4. 从模板恢复 user.md（对玩家的认知）
    user_default = f"{agent_path}/user.default.md"
    user_path = f"{agent_path}/user.md"
    if os.path.exists(user_default):
        try:
            shutil.copy2(user_default, user_path)
            print(f"  已恢复: user.md")
        except Exception as e:
            print(f"  恢复失败 user.md: {e}")


@cl.on_chat_start
async def on_chat_start():
    """聊天开始时的初始化 - 重置记忆、显示默认开场并写入所有角色历史"""

    # 重置所有角色的记忆（确保每次新游戏都是全新开始）
    all_agents = ["lilith", "ruri", "mitsuki", "narrator"]
    print(f"\n{'='*40}")
    print("新游戏开始，重置所有角色记忆...")
    print(f"{'='*40}\n")
    for agent_name in all_agents:
        print(f"[{agent_name}]")
        reset_agent_memory(agent_name)
    print(f"\n{'='*40}")
    print("记忆重置完成")
    print(f"{'='*40}\n")

    default_opening = """**私立桜庭学园 · 4月的清晨**

樱花瓣随风飘进教室的窗户。
你坐在靠窗的座位上，看着窗外熟悉的景色。
新学期刚开始，距离毕业还有三个月。

讲台上，班主任拍了拍手：
"今天有两位转学生加入我们班级。"

你的心跳莫名加速了一点。
也许是因为春风，也许是因为——

*你会怎么做？*"""

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
        "visible_to": ["lilith", "ruri", "mitsuki", "narrator"],
    }

    # 写入所有角色的 jsonl
    all_agents = ["lilith", "ruri", "mitsuki", "narrator"]
    for agent_name in all_agents:
        raw_path = f"agents/{agent_name}/memory/raw/{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        os.makedirs(os.path.dirname(raw_path), exist_ok=True)
        with open(raw_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(opening_message, ensure_ascii=False) + "\n")


@cl.on_message
async def on_message(message: cl.Message):
    """处理用户消息"""
    user_input = message.content

    # 1. 先调用 narrator（导演）决定场景和 targets
    narrator_history = broadcaster.load_recent_history("narrator", limit=10)
    if narrator_history:
        narrator_input = f"最近对话历史:\n\n{narrator_history}\n\n---\n\n玩家新消息: {user_input}"
    else:
        narrator_input = user_input

    print("[导演] narrator 正在判断场景和 targets...")
    narrator_response = await agent_manager.run_agent("narrator", narrator_input)
    narrator_content = clean_response(narrator_response.content)

    # 解析 narrator 的 TARGETS 指令
    targets = []
    scene_description = narrator_content

    # 查找 TARGETS: [...] 格式
    targets_match = re.search(r'TARGETS:\s*\[([^\]]*)\]', narrator_content, re.IGNORECASE)
    if targets_match:
        targets_str = targets_match.group(1)
        # 解析角色列表
        targets = [t.strip().lower() for t in targets_str.split(',') if t.strip()]
        # 移除 TARGETS 行，保留场景描述
        scene_description = re.sub(r'TARGETS:\s*\[[^\]]*\]\n?', '', narrator_content, flags=re.IGNORECASE).strip()

    # 过滤有效角色
    valid_agents = ["lilith", "ruri", "mitsuki"]
    targets = [t for t in targets if t in valid_agents]

    print(f"[导演] narrator 决定 targets: {targets}")
    if scene_description:
        print(f"[导演] 场景描述:\n{scene_description[:200]}...")
    print("-" * 40)

    # 2. 广播玩家消息到 narrator（导演已看到）
    await broadcaster.broadcast_player_message(["narrator"], user_input)

    # 3. 如果有场景描述，广播并展示
    if scene_description:
        await broadcaster.broadcast_agent_response("narrator", ["narrator"], scene_description)
        await cl.Message(content=scene_description, author="Narrator").send()

    # 4. 如果没有角色需要回应，结束
    if not targets:
        print("[导演] 无角色需要回应")
        return

    # 5. 并行调用 targets 中的角色
    async def run_single_agent(agent_name: str) -> tuple:
        """运行单个 agent 并返回结果"""
        try:
            # 加载该角色的历史上下文
            history = broadcaster.load_recent_history(agent_name, limit=10)

            # 构建完整输入（包含历史、场景描述）
            parts = []
            if history:
                parts.append(f"最近对话历史:\n\n{history}")
            if scene_description:
                parts.append(f"当前场景:\n\n{scene_description}")
            parts.append(f"玩家新消息: {user_input}")
            full_input = "\n\n---\n\n".join(parts)

            # 调用 agent
            response = await agent_manager.run_agent(agent_name, full_input)
            return agent_name, response.content

        except Exception as e:
            print(f"Agent {agent_name} 运行失败: {e}")
            return agent_name, f"[错误: {str(e)}]"

    # 并行执行所有目标 agents
    agent_tasks = [run_single_agent(name) for name in targets]
    results = await asyncio.gather(*agent_tasks)

    # 6. 广播角色回应到各自 jsonl
    for agent_name, response in results:
        await broadcaster.broadcast_agent_response(agent_name, targets, response)

    # 7. 展示给玩家
    for agent_name, response in results:
        cleaned_response = clean_response(response)
        if cleaned_response:
            await cl.Message(
                content=cleaned_response,
                author=agent_name.capitalize(),
            ).send()


@cl.on_chat_end
async def on_chat_end():
    """聊天结束时的清理"""
    pass


if __name__ == "__main__":
    # 本地运行调试
    print("MemoBot 启动...")
    print(f"时间: {datetime.now().isoformat()}")
    print(f"可用角色: lilith (魅魔), ruri (天降巫女), mitsuki (青梅竹马), narrator (旁白)")
