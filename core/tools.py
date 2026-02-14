"""Agent Tools - 所有角色共享的工具"""

import json
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


def _read_title(file_path: str, default: str) -> str:
    """读取文件首行标题，不存在则返回默认值。"""
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
            if first_line.startswith("# "):
                return first_line
    return default


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

    # 预计算该角色的白名单字段
    status_fields = STATUS_FIELDS.get(agent_name, [])
    user_fields = USER_FIELDS.get(agent_name, [])
    status_fields_str = "、".join(status_fields)
    user_fields_str = "、".join(user_fields)

    async def _update_notes(
        memory: str = "",
        status: str = "",
        player: str = "",
    ) -> str:
        """一次性更新记忆、状态、玩家认知。不需要更新的参数留空。

        Args:
            memory (str): 追加到长期记忆，用第一人称记录事件和感受。留空则不更新。
            status (str): 更新状态字段，JSON 格式 {{"字段": "内容"}}，会覆盖原内容。允许的字段：{status_fields}。留空则不更新。
            player (str): 追加玩家认知，JSON 格式 {{"字段": "内容"}}，只写新增部分。允许的字段：{player_fields}。留空则不更新。
        """
        blocked = _check_limit()
        if blocked:
            return blocked

        results: list[str] = []

        # --- memory: 追加到 Memory.md ---
        if memory and memory.strip():
            try:
                routing_logger.info(
                    f"[Tool] {agent_name} update_notes: memory"
                )
                memory_path = f"agents/{agent_name}/memory/Memory.md"
                os.makedirs(os.path.dirname(memory_path), exist_ok=True)
                clean = memory.replace("\\n", "\n")
                if os.path.exists(memory_path):
                    with open(memory_path, "a", encoding="utf-8") as f:
                        f.write(f"\n\n{clean}")
                else:
                    with open(memory_path, "w", encoding="utf-8") as f:
                        f.write(f"# {agent_name} 的长期记忆\n\n{clean}")
                results.append(f"✓ memory 已追加")
            except Exception as e:
                routing_logger.error(
                    f"[Tool] {agent_name} update_notes memory 出错: {e}"
                )
                results.append(f"✗ memory 出错: {e}")

        # --- status: 覆盖模式，JSON 解析 ---
        if status and status.strip():
            try:
                updates = json.loads(status)
                if not isinstance(updates, dict):
                    results.append("✗ status 格式错误，需要 JSON 对象")
                else:
                    status_path = f"agents/{agent_name}/status.md"
                    title = _read_title(status_path, "# 我的状态")
                    allowed = STATUS_FIELDS.get(agent_name, [])
                    for field, content in updates.items():
                        if field not in allowed:
                            results.append(f"✗ status[{field}] 不在允许字段中，已跳过")
                            continue
                        result = _update_section_file(
                            status_path, field, str(content), allowed, title
                        )
                        routing_logger.info(
                            f"[Tool] {agent_name} update_notes status[{field}]: {result}"
                        )
                        results.append(f"✓ status[{field}]: {result}")
            except json.JSONDecodeError:
                results.append("✗ status JSON 解析失败，请检查格式")
            except Exception as e:
                routing_logger.error(
                    f"[Tool] {agent_name} update_notes status 出错: {e}"
                )
                results.append(f"✗ status 出错: {e}")

        # --- player: 追加模式，JSON 解析 ---
        if player and player.strip():
            try:
                updates = json.loads(player)
                if not isinstance(updates, dict):
                    results.append("✗ player 格式错误，需要 JSON 对象")
                else:
                    user_path = f"agents/{agent_name}/user.md"
                    title = _read_title(user_path, "# 玩家档案")
                    allowed = USER_FIELDS.get(agent_name, [])
                    for field, content in updates.items():
                        if field not in allowed:
                            results.append(f"✗ player[{field}] 不在允许字段中，已跳过")
                            continue
                        result = _append_section_file(
                            user_path, field, str(content), allowed, title
                        )
                        routing_logger.info(
                            f"[Tool] {agent_name} update_notes player[{field}]: {result}"
                        )
                        results.append(f"✓ player[{field}]: {result}")
            except json.JSONDecodeError:
                results.append("✗ player JSON 解析失败，请检查格式")
            except Exception as e:
                routing_logger.error(
                    f"[Tool] {agent_name} update_notes player 出错: {e}"
                )
                results.append(f"✗ player 出错: {e}")

        if not results:
            return "所有参数为空，未执行任何更新。"

        routing_logger.info(
            f"[Tool] {agent_name} update_notes 完成: {'; '.join(results)}"
        )
        return "\n".join(results)

    # 动态注入白名单字段到 docstring
    _update_notes.__doc__ = _update_notes.__doc__.replace(
        "{status_fields}", status_fields_str
    ).replace(
        "{player_fields}", user_fields_str
    )
    _update_notes.__name__ = "update_notes"
    update_notes = tool(_update_notes)

    tools = [update_notes]
    return tools, _reset_counter
