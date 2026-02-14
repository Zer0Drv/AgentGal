"""Agent Tools - 所有角色共享的工具"""

import os
from typing import List, Callable
from agno.tools import tool
from .routing_logger import routing_logger
from .vector_store import vector_store

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


def _update_section_file(
    file_path: str,
    section: str,
    content: str,
    allowed_sections: list[str],
    title_line: str,
) -> str:
    """通用的 section 式文件更新（白名单校验 + 覆盖指定章节）

    Args:
        file_path: 文件路径
        section: 要更新的 ## 标题
        content: 新内容（覆盖该章节）
        allowed_sections: 允许的字段白名单
        title_line: 文件首行标题（如 "# 莉莉丝的状态"）

    Returns:
        成功/失败消息
    """
    # 白名单校验
    if section not in allowed_sections:
        return (
            f"错误：不允许的字段「{section}」。"
            f"只能更新以下字段：{', '.join(allowed_sections)}"
        )

    # LLM 经常传入字面 \n 而非真换行
    content = content.replace("\\n", "\n")

    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    # 读取现有内容
    existing_content = ""
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            existing_content = f.read()

    # 解析章节
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

    # 确保所有白名单字段都存在且保持顺序
    for field in allowed_sections:
        if field not in section_order:
            section_order.append(field)
            sections[field] = "（暂无）"

    # 覆盖更新
    sections[section] = content

    # 重新组装（只保留白名单内的字段，按白名单顺序）
    lines = [title_line, ""]
    for sec in allowed_sections:
        if sec in sections:
            lines.append(f"## {sec}")
            lines.append(sections[sec])
            lines.append("")

    with open(file_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return f"已更新 [{section}]: {content[:50]}..."


def create_tools_for_agent(agent_name: str) -> List[Callable]:
    """
    为指定角色创建工具函数列表

    Args:
        agent_name: 角色名称

    Returns:
        该角色的工具函数列表
    """

    @tool
    async def search_memory(query: str, limit: int = 10) -> str:
        """语义搜索自己的长期记忆，用于回忆过去发生的事情。

        Args:
            query (str): 搜索查询内容（如"和玩家在天台的对话"）
            limit (int): 返回结果数量，默认5条
        """
        try:
            routing_logger.info(f"[Tool] {agent_name} 调用 search_memory: {query}")
            results = await vector_store.search(agent_name, query, limit=limit)
            if not results:
                return "没有找到相关记忆。"
            lines = ["找到以下相关记忆："]
            for i, r in enumerate(results, 1):
                lines.append(f"\n{i}. {r['content'][:200]}")
            result_text = "\n".join(lines)
            routing_logger.info(
                f"[Tool] {agent_name} search_memory 返回 {len(results)} 条"
            )
            return result_text
        except Exception as e:
            routing_logger.error(f"[Tool] {agent_name} search_memory 出错: {e}")
            return f"搜索记忆时出错: {e}"

    @tool
    async def update_memory(content: str) -> str:
        """追加长期记忆文件 Memory.md。写入时只记事件和情感，不写环境描写。

        Args:
            content (str): 要写入记忆的内容（聚焦事件和内心感受，不要写环境描写）
        """
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

            # 后台增量 embedding（不阻塞 tool 返回）
            full_content = ""
            with open(memory_path, "r", encoding="utf-8") as f:
                full_content = f.read()
            vector_store.schedule_incremental_sync(agent_name, full_content)

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
        """更新对玩家的认知（印象、了解到的信息、在意的细节）。当玩家透露个人信息、做出让你在意的举动时调用。

        Args:
            section (str): 要更新的字段名（只允许：{fields}）
            content (str): 该字段的新内容（会覆盖原内容）
        """
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
            result = _update_section_file(
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

    tools = [search_memory, update_memory, update_status, update_player]
    return tools
