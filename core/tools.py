"""Agent Tools - 所有角色共享的工具"""

import os
import json
from datetime import datetime
from typing import List, Callable
from .vector_store import vector_store


def create_tools_for_agent(agent_name: str) -> List[Callable]:
    """
    为指定角色创建工具函数列表

    Args:
        agent_name: 角色名称

    Returns:
        该角色的工具函数列表
    """

    async def search_memory(query: str, limit: int = 5) -> str:
        """语义搜索自己的长期记忆

        Args:
            query: 搜索查询内容
            limit: 返回结果数量，默认5条
        """
        try:
            results = await vector_store.search(agent_name, query, limit=limit)

            if not results:
                return "没有找到相关记忆。"

            lines = ["找到以下相关记忆："]
            for i, r in enumerate(results, 1):
                lines.append(f"\n{i}. [{r['source']}] {r['content'][:200]}...")

            return "\n".join(lines)

        except Exception as e:
            return f"搜索记忆时出错: {e}"

    async def update_memory(content: str, mode: str = "append") -> str:
        """追加或编辑长期记忆文件 Memory.md

        Args:
            content: 要写入记忆的内容
            mode: 写入模式，'append'追加或'replace'替换，默认append
        """
        try:
            memory_path = f"agents/{agent_name}/memory/Memory.md"
            os.makedirs(os.path.dirname(memory_path), exist_ok=True)

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

            if mode == "replace":
                with open(memory_path, "w", encoding="utf-8") as f:
                    f.write(f"# {agent_name} 的长期记忆\n\n")
                    f.write(f"## 更新于 {timestamp}\n\n")
                    f.write(content)
            else:
                if os.path.exists(memory_path):
                    with open(memory_path, "a", encoding="utf-8") as f:
                        f.write(f"\n\n## {timestamp}\n\n{content}")
                else:
                    with open(memory_path, "w", encoding="utf-8") as f:
                        f.write(f"# {agent_name} 的长期记忆\n\n")
                        f.write(f"## {timestamp}\n\n{content}")

            # 同步到向量库
            await vector_store.init_agent_table(agent_name)
            await vector_store.add_memory(
                agent_name=agent_name,
                content=content,
                source="Memory.md",
            )

            return f"记忆已更新: {content[:50]}..."

        except Exception as e:
            return f"更新记忆时出错: {e}"

    async def summarize_today(summary: str) -> str:
        """生成今日对话摘要并存入 daily 目录

        Args:
            summary: 今日对话摘要内容
        """
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            daily_path = f"agents/{agent_name}/memory/daily/{today}.md"

            os.makedirs(os.path.dirname(daily_path), exist_ok=True)

            timestamp = datetime.now().strftime("%H:%M")

            if os.path.exists(daily_path):
                with open(daily_path, "a", encoding="utf-8") as f:
                    f.write(f"\n\n## {timestamp}\n\n{summary}")
            else:
                with open(daily_path, "w", encoding="utf-8") as f:
                    f.write(f"# {today} 的摘要\n\n## {timestamp}\n\n{summary}")

            return f"今日摘要已保存: {summary[:50]}..."

        except Exception as e:
            return f"生成摘要时出错: {e}"

    return [
        search_memory,
        update_memory,
        summarize_today,
    ]
