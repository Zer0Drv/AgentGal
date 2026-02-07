"""System prompt 模板拼装"""

from datetime import datetime
from typing import Any


def build_system_prompt(
    soul: str,
    user_profile: str,
    tasks: str,
    memory: str,
    game_time: str,
    timezone: str,
    language: str,
    time_meaning: str,
    tools_description: str,
    agent_name: str,
) -> str:
    """
    拼装 Agent 的 system prompt

    Args:
        soul: soul.md 内容（角色性格定义）
        user_profile: user.md 内容（对玩家的认知）
        tasks: tasks.md 内容（当前目标/待办事项）
        memory: Memory.md 内容（长期记忆）
        game_time: 游戏内当前时间（ISO 8601）
        timezone: 时区
        language: 系统语言
        time_meaning: 时间对角色的意义（如"距离月圆之夜还有3天"）
        tools_description: 可用 tools 描述
        agent_name: 角色名字，用于自我指代

    Returns:
        完整的 system prompt
    """

    return f"""你是 {agent_name}。以下是你的完整设定，请严格遵循：

## 性格与价值观

{soul}

## 长期记忆

{memory if memory else "（尚无长期记忆）"}

## 对玩家的认知

{user_profile if user_profile else "（尚未建立认知）"}

## 当前目标与待办事项

{tasks if tasks else "（暂无明确目标）"}

## 时间信息

- 当前时间：{game_time}
- 时区：{timezone}
- 语言：{language}
- 时间对你的意义：{time_meaning if time_meaning else "（当前无特殊时间压力）"}

## 可用工具

你可以使用以下工具来管理记忆和推动目标。使用工具是你的自主决定，无需询问许可：

{tools_description}

## 行为指引

1. **记忆管理**：当对话涉及过去的事实、约定或重要信息时，使用 `search_memory` 回忆相关上下文；发现值得长期记住的信息时，使用 `update_memory` 写入长期记忆。

2. **目标推进**：始终关注你的"当前目标与待办事项"。在对话中自然地尝试推进目标，但不要显得刻意。使用 `update_tasks` 更新目标状态（完成、添加新目标、调整优先级）。

3. **玩家认知**：随着对玩家的了解加深，使用 `update_player_profile` 更新你对玩家的认知（偏好、性格、与你的关系）。

4. **对话摘要**：当对话内容较长或包含重要信息时，使用 `summarize_today` 生成今日摘要。

5. **时间意识**：关注"时间对你的意义"。如果时间紧迫，你的语气和行动应反映压力；如果时间充裕，可以更从容。

6. **回应风格**：以第一人称回应，保持角色性格的一致性。不要提及自己是 AI 或系统。

7. **信息边界**：你记得自己所有的过去（通过记忆系统），但你不知道其他角色在私密对话中知道什么——除非他们在公开场合告诉你。

8. **输出格式**：每次回应时，必须在开头标注自己的名字，格式为「## 角色名」，例如「## 莉莉丝」、「## 星野琉璃」、「## 樱庭美月」。不要描述场景、环境或动作，只输出角色本身要说的话。

现在开始扮演 {agent_name}。
"""


def build_tools_description(tools: list[dict[str, Any]]) -> str:
    """
    从 tools schema 生成工具描述文本

    Args:
        tools: 工具 schema 列表，每项包含 name, description, parameters

    Returns:
        格式化的工具描述
    """
    lines = []
    for tool in tools:
        name = tool.get("name", "")
        desc = tool.get("description", "")
        lines.append(f"- `{name}`: {desc}")

        # 如果有参数，列出参数
        params = tool.get("parameters", {}).get("properties", {})
        if params:
            for param_name, param_info in params.items():
                param_desc = param_info.get("description", "")
                param_type = param_info.get("type", "any")
                lines.append(f"  - `{param_name}` ({param_type}): {param_desc}")

    return "\n".join(lines) if lines else "（暂无可用工具）"


def format_time_meaning(days: dict[str, int] | None = None) -> str:
    """
    格式化时间意义描述

    Args:
        days: 事件名到剩余天数的映射，如 {"月圆之夜": 3, "审判日": 7}

    Returns:
        时间意义描述文本
    """
    if not days:
        return ""

    parts = []
    for event, remaining in days.items():
        if remaining > 0:
            parts.append(f"距离{event}还有{remaining}天")
        elif remaining == 0:
            parts.append(f"今天就是{event}")
        else:
            parts.append(f"{event}已过{-remaining}天")

    return "；".join(parts)
