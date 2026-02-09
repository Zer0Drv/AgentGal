"""Agent Tools - 所有角色共享的工具"""

import os
from typing import List, Callable
from agno.tools import tool
from .routing_logger import routing_logger


def create_tools_for_agent(agent_name: str) -> List[Callable]:
    """
    为指定角色创建工具函数列表

    Args:
        agent_name: 角色名称

    Returns:
        该角色的工具函数列表
    """

    @tool
    async def update_memory(content: str, mode: str = "append") -> str:
        """追加或编辑长期记忆文件 Memory.md

        Args:
            content: 要写入记忆的内容
            mode: 写入模式，'append'追加或'replace'替换，默认append
        """
        try:
            routing_logger.info(f"[Tool] {agent_name} 调用 update_memory")
            memory_path = f"agents/{agent_name}/memory/Memory.md"
            os.makedirs(os.path.dirname(memory_path), exist_ok=True)

            if mode == "replace":
                with open(memory_path, "w", encoding="utf-8") as f:
                    f.write(f"# {agent_name} 的长期记忆\n\n")
                    f.write(content)
            else:
                if os.path.exists(memory_path):
                    with open(memory_path, "a", encoding="utf-8") as f:
                        f.write(f"\n\n{content}")
                else:
                    with open(memory_path, "w", encoding="utf-8") as f:
                        f.write(f"# {agent_name} 的长期记忆\n\n")
                        f.write(content)

            return f"记忆已更新: {content[:50]}..."

        except Exception as e:
            routing_logger.info(f"[Tool] {agent_name} update_memory 出错: {e}")
            return f"更新记忆时出错: {e}"

    return [
        update_memory,
    ]
