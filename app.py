"""Chainlit 入口"""

import asyncio
import json
import os

import chainlit as cl
from dotenv import load_dotenv

from engine.agent_manager import (
    call_narrator_and_route,
    generate_choices,
    run_agent_in_scene,
)
from engine.config import CHARACTERS_DIR, CONSOLIDATION_INTERVAL, get_agent_names
from engine.message_router import message_router

from game.save_manager import (
    export_save_archive,
    has_existing_save,
    import_save_archive,
    list_save_archives,
    load_conversation_history,
    load_story_file,
    reset_game,
)

from memory.consolidator import memory_consolidator

# 加载环境变量
load_dotenv()

_LAST_CHOICES_FILE = CHARACTERS_DIR / "last_choices.json"


def _save_last_choices(choices: list[str]) -> None:
    """持久化最新选项到文件"""
    _LAST_CHOICES_FILE.write_text(json.dumps(choices, ensure_ascii=False), encoding="utf-8")


def _load_last_choices() -> list[str]:
    """加载最新选项，不存在或失败返回空列表"""
    try:
        return json.loads(_LAST_CHOICES_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _clear_last_choices() -> None:
    """清除保存的选项"""
    _LAST_CHOICES_FILE.unlink(missing_ok=True)


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
# on_chat_start
# =============================================================================


STORY_OPTIONS = [
    cl.Action(name="school", payload={"story": "school"}, label="🏫 私立城川中学 · 青春校园"),
    cl.Action(name="modern", payload={"story": "modern"}, label="🌆 不期而遇 · 现代都市"),
]


async def _ask_story_choice() -> str:
    """弹出故事选择，返回 story_id；超时或取消则默认 school"""
    res = await cl.AskActionMessage(
        content="请选择你想要进入的故事世界：",
        actions=STORY_OPTIONS,
        timeout=600,
    ).send()
    if res and res.get("payload"):
        return res["payload"].get("story", "school")
    return "school"


async def _send_opening_messages(story_id: str, intro_text: str, opening_text: str) -> None:
    """发送玩法介绍与故事开场白"""
    if intro_text:
        await cl.Message(content=intro_text, author="Narrator").send()
    if opening_text:
        await cl.Message(content=opening_text, author="Narrator").send()
        opening_choices_text = load_story_file(story_id, "opening_choices.txt")
        choices = [line.strip() for line in opening_choices_text.splitlines() if line.strip()]
        if choices:
            _save_last_choices(choices)
            await _send_choices_message(choices)


async def _send_choices_message(choices: list[str]) -> None:
    """将选项列表渲染为按钮并发送"""
    actions = [
        cl.Action(name="choice", payload={"text": c}, label=c)
        for c in choices
    ]
    await cl.Message(
        content="你可以选择接下来的行动，或者直接输入你想做的事。选项只是参考，你可以做任何事情。",
        actions=actions,
    ).send()


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

    # 恢复上次保存的选项
    saved_choices = _load_last_choices()
    if saved_choices:
        await _send_choices_message(saved_choices)


@cl.on_chat_start
async def on_chat_start():
    """聊天开始时的初始化 - 有记忆直接加载"""
    has_save = has_existing_save()

    if not has_save:
        story_id = await _ask_story_choice()
        intro_text, opening_text = await _handle_new_game(story_id)
        await _send_opening_messages(story_id, intro_text, opening_text)
    else:
        await _handle_continue_game()


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
    _clear_last_choices()
    intro_text, opening_text = await reset_game(story_id)
    await _send_opening_messages(story_id, intro_text, opening_text)
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

    # 1. 调用 narrator 获取路由决策和场景描述
    targets, scene_description, is_narrator_valid = await call_narrator_and_route(
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

    # 5. 顺序处理目标角色，每个完成后立即推送到前端
    agent_responses: list[tuple[str, str]] = []
    for agent_name in targets:
        try:
            response = await run_agent_in_scene(
                agent_name, targets, user_input, scene_summary=scene_description
            )
            if response:
                agent_responses.append((agent_name, response))
                await cl.Message(content=response, author=agent_name.capitalize()).send()
        except Exception as e:
            print(f"Agent {agent_name} 运行失败: {e}")
            await cl.Message(content=f"[错误: {str(e)}]", author=agent_name.capitalize()).send()

    # 6. 生成玩家可选行动
    if agent_responses:
        choices = await generate_choices(scene_description, agent_responses)
        if choices:
            _save_last_choices(choices)
            await _send_choices_message(choices)

    # 7. 每 N 轮触发记忆整理（后台执行，不阻塞用户交互）
    print(
        f"[调试] 当前轮次: {message_counter}, CONSOLIDATION_INTERVAL: {CONSOLIDATION_INTERVAL}, 是否触发: {message_counter % CONSOLIDATION_INTERVAL == 0}"
    )
    if message_counter % CONSOLIDATION_INTERVAL == 0:
        all_agents = get_agent_names()
        print(f"[调试] 触发记忆整理，目标角色: {all_agents}")
        asyncio.create_task(memory_consolidator.consolidate_all(all_agents))


@cl.action_callback("choice")
async def on_choice_action(action: cl.Action):
    """玩家点击选项按钮时，将选项文本作为新消息处理"""
    choice_text = action.payload.get("text", "")
    if choice_text:
        # 构造一个 Message 对象交给 on_message 处理
        msg = cl.Message(content=choice_text, author="User")
        await msg.send()
        await on_message(msg)

        
if __name__ == "__main__":
    # 本地运行调试
    from datetime import datetime

    print("AgentGal 启动...")
    print(f"时间: {datetime.now().isoformat()}")
    agents = get_agent_names()
    print(f"可用角色: {', '.join(agents)}")
