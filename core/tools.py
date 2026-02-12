"""Agent Tools - 所有角色共享的工具"""

import os
from typing import List, Callable
from agno.tools import tool
from .routing_logger import routing_logger
from .vector_store import vector_store


def create_tools_for_agent(agent_name: str) -> List[Callable]:
    """
    为指定角色创建工具函数列表

    Args:
        agent_name: 角色名称

    Returns:
        该角色的工具函数列表
    """

    @tool
    async def search_memory(query: str, limit: int = 5) -> str:
        """语义搜索自己的长期记忆，用于回忆过去发生的事情。

        Args:
            query: 搜索查询内容（如"和玩家在天台的对话"）
            limit: 返回结果数量，默认5条
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
                f"[Tool] {agent_name} search_memory 返回 {len(results)} 条:\n{result_text}"
            )
            return result_text
        except Exception as e:
            routing_logger.error(f"[Tool] {agent_name} search_memory 出错: {e}")
            return f"搜索记忆时出错: {e}"

    @tool
    async def update_memory(content: str) -> str:
        """追加长期记忆文件 Memory.md。写入时只记事件和情感，不写环境描写。

        Args:
            content: 要写入记忆的内容（聚焦事件和内心感受，不要写环境描写）
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

    return [
        search_memory,
        update_memory,
    ]
