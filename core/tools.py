"""Agent Tools - 所有角色共享的工具"""

import os
from typing import List, Callable
from agno.tools import tool
from .routing_logger import routing_logger

# 每个角色 status.md 允许的字段白名单（对应 ## 标题）
STATUS_FIELDS: dict[str, list[str]] = {
    "lilith": ["身份", "心境", "我和他", "在意的事", "打算"],
    "mitsuki": ["心境", "我和他", "在意的事", "打算"],
    "narrator": ["故事阶段", "当前时间", "场景", "正在发酵的冲突", "伏笔"],
}

# 每个角色 user.md 允许的字段白名单
USER_FIELDS: dict[str, list[str]] = {
    "lilith": ["印象", "了解到的事", "在意的细节"],
    "mitsuki": ["印象", "了解到的事", "在意的细节"],
    "narrator": ["玩家风格", "关键选择", "当前倾向"],
}


def _parse_section_file(
    file_path: str,
    allowed_sections: list[str],
) -> dict[str, str]:
    """解析 section 式文件，返回 {字段名: 内容} 字典。

    自动确保所有白名单字段存在。
    """
    existing_content = ""
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            existing_content = f.read()

    sections: dict[str, str] = {}
    section_order: list[str] = []
    current_sec = None
    current_lines: list[str] = []

    for line in existing_content.split("\n"):
        if line.startswith("## "):
            if current_sec:
                sections[current_sec] = "\n".join(current_lines).strip()
            current_sec = line[3:].strip()
            if current_sec not in section_order:
                section_order.append(current_sec)
            current_lines = []
        elif not line.startswith("# "):
            current_lines.append(line)

    if current_sec:
        sections[current_sec] = "\n".join(current_lines).strip()

    for field in allowed_sections:
        if field not in section_order:
            section_order.append(field)
            sections[field] = "（暂无）"

    return sections


def _write_section_file(
    file_path: str,
    sections: dict[str, str],
    allowed_sections: list[str],
    title_line: str,
) -> None:
    """将 sections 字典写回文件，按白名单顺序排列。"""
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    lines = [title_line, ""]
    for sec in allowed_sections:
        if sec in sections:
            lines.append(f"## {sec}")
            lines.append(sections[sec])
            lines.append("")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _update_section_file(
    file_path: str,
    section: str,
    content: str,
    allowed_sections: list[str],
    title_line: str,
) -> str:
    """覆盖模式更新 section 式文件（用于 update_status）。"""
    if section not in allowed_sections:
        return (
            f"错误：不允许的字段「{section}」。"
            f"只能更新以下字段：{', '.join(allowed_sections)}"
        )

    content = content.replace("\\n", "\n")
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    sections = _parse_section_file(file_path, allowed_sections)

    old_content = sections.get(section, "").strip()
    new_content = content.strip()
    if old_content == new_content:
        return f"[{section}] 内容未变化，无需更新。"

    sections[section] = content
    _write_section_file(file_path, sections, allowed_sections, title_line)
    return f"已更新 [{section}]: {content[:50]}..."


def _append_section_file(
    file_path: str,
    section: str,
    content: str,
    allowed_sections: list[str],
    title_line: str,
) -> str:
    """追加模式更新 section 式文件（用于 update_player）。

    只追加新信息，已存在的内容自动跳过。
    """
    if section not in allowed_sections:
        return (
            f"错误：不允许的字段「{section}」。"
            f"只能更新以下字段：{', '.join(allowed_sections)}"
        )

    content = content.replace("\\n", "\n")
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    sections = _parse_section_file(file_path, allowed_sections)
    old_content = sections.get(section, "").strip()

    # 如果字段为空或占位符，直接写入
    if not old_content or old_content == "（暂无）":
        sections[section] = content.strip()
        _write_section_file(file_path, sections, allowed_sections, title_line)
        return f"已更新 [{section}]: {content[:50]}..."

    # 去重：新内容已是已有内容的子串 → 跳过
    new_stripped = content.strip()
    if new_stripped in old_content:
        return f"[{section}] 内容已存在，无需重复追加。"

    # 追加到末尾（换行分隔）
    sections[section] = old_content + "\n" + new_stripped
    _write_section_file(file_path, sections, allowed_sections, title_line)
    return f"已追加 [{section}]: {content[:50]}..."


def create_tools_for_agent(
    agent_name: str,
    tool_call_limit: int = 5,
) -> tuple[List[Callable], Callable[[], None]]:
    """
    为指定角色创建工具函数列表，并附带 per-turn 调用计数器。

    Args:
        agent_name: 角色名称
        tool_call_limit: 单轮最大工具调用次数

    Returns:
        (工具函数列表, 重置计数器的回调函数)
    """
    # 闭包共享的调用计数器
    _counter = {"count": 0, "limit": tool_call_limit}

    def _reset_counter() -> None:
        """每轮对话开始前调用，重置工具计数器。"""
        _counter["count"] = 0

    def _check_limit() -> str | None:
        """检查是否超限，超限返回提示文本，否则返回 None。"""
        _counter["count"] += 1
        if _counter["count"] > _counter["limit"]:
            return (
                "本轮工具调用次数已达上限，请直接基于已有信息生成回应，"
                "不要再调用任何工具。"
            )
        return None

    @tool
    async def update_memory(content: str) -> str:
        """追加长期记忆文件 Memory.md。写入时只记事件和情感，不写环境描写。

        Args:
            content (str): 要写入记忆的内容（聚焦事件和内心感受，不要写环境描写）
        """
        blocked = _check_limit()
        if blocked:
            return blocked
        try:
            routing_logger.info(f"[Tool] {agent_name} 调用 update_memory")
            memory_path = f"agents/{agent_name}/memory/Memory.md"
            os.makedirs(os.path.dirname(memory_path), exist_ok=True)

            # LLM 经常传入字面 \n 而非真换行，统一修复
            content = content.replace("\\n", "\n")

            if os.path.exists(memory_path):
                with open(memory_path, "a", encoding="utf-8") as f:
                    f.write(f"\n\n{content}")
            else:
                with open(memory_path, "w", encoding="utf-8") as f:
                    f.write(f"# {agent_name} 的长期记忆\n\n")
                    f.write(content)

            routing_logger.info(f"[Tool] {agent_name} update_memory 完成")
            return f"记忆已更新: {content[:50]}..."

        except Exception as e:
            routing_logger.info(f"[Tool] {agent_name} update_memory 出错: {e}")
            return f"更新记忆时出错: {e}"

    # --- update_status：先设 docstring 再 @tool，否则 {fields} 不会被替换 ---
    status_fields = STATUS_FIELDS.get(agent_name, [])
    status_fields_str = " / ".join(status_fields)

    async def _update_status(section: str, content: str) -> str:
        """更新自己的内心状态（心境、关系认知、打算等）。当情绪变化、关系转变、做了重要决定时调用。

        Args:
            section (str): 要更新的字段名（只允许：{fields}）
            content (str): 该字段的新内容（会覆盖原内容，用简洁的第一人称描述）
        """
        blocked = _check_limit()
        if blocked:
            return blocked
        try:
            routing_logger.info(f"[Tool] {agent_name} 调用 update_status: {section}")
            status_path = f"agents/{agent_name}/status.md"
            allowed = STATUS_FIELDS.get(agent_name, [])
            title = "# 我的状态"
            if os.path.exists(status_path):
                with open(status_path, "r", encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line.startswith("# "):
                        title = first_line
            result = _update_section_file(
                status_path, section, content, allowed, title
            )
            routing_logger.info(f"[Tool] {agent_name} update_status 完成: {result}")
            return result
        except Exception as e:
            routing_logger.error(f"[Tool] {agent_name} update_status 出错: {e}")
            return f"更新状态时出错: {e}"

    _update_status.__doc__ = _update_status.__doc__.replace("{fields}", status_fields_str)
    _update_status.__name__ = "update_status"
    update_status = tool(_update_status)

    # --- update_player：同理，先设 docstring 再 @tool ---
    user_fields = USER_FIELDS.get(agent_name, [])
    user_fields_str = " / ".join(user_fields)

    async def _update_player(section: str, content: str) -> str:
        """追加对玩家的新认知。只写这一轮新了解到的内容，不要重复已有信息。

        Args:
            section (str): 要更新的字段名（只允许：{fields}）
            content (str): 这次新了解到的内容（只写新增部分，已有的不要重复写）
        """
        blocked = _check_limit()
        if blocked:
            return blocked
        try:
            routing_logger.info(f"[Tool] {agent_name} 调用 update_player: {section}")
            user_path = f"agents/{agent_name}/user.md"
            allowed = USER_FIELDS.get(agent_name, [])
            title = "# 玩家档案"
            if os.path.exists(user_path):
                with open(user_path, "r", encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line.startswith("# "):
                        title = first_line
            result = _append_section_file(
                user_path, section, content, allowed, title
            )
            routing_logger.info(f"[Tool] {agent_name} update_player 完成: {result}")
            return result
        except Exception as e:
            routing_logger.error(f"[Tool] {agent_name} update_player 出错: {e}")
            return f"更新玩家档案时出错: {e}"

    _update_player.__doc__ = _update_player.__doc__.replace("{fields}", user_fields_str)
    _update_player.__name__ = "update_player"
    update_player = tool(_update_player)

    tools = [update_memory, update_status, update_player]
    return tools, _reset_counter
