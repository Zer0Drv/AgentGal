"""向量存储 - EverMemOS 后端，只负责存入和搜索"""

import os
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any
from log_config.routing import routing_logger
from engine.config import character_path

# 可选依赖：evermemos
try:
    from evermemos import EverMemOS

    _HAS_EVERMEMOS = True
except ImportError:
    _HAS_EVERMEMOS = False
    EverMemOS = None


class VectorStore:
    """EverMemOS 向量存储。职责：存入（add/schedule_add）和搜索（search）。"""

    def __init__(self):
        self._client = None
        self._bg_tasks: set[asyncio.Task] = set()

    def _get_client(self):
        """获取 EverMemOS 客户端（延迟初始化）"""
        if self._client is None:
            if not _HAS_EVERMEMOS:
                raise ImportError("evermemos SDK not installed. Run: uv add evermemos")
            api_key = os.getenv("EVERMEMOS_API_KEY")
            if not api_key:
                raise ValueError("EVERMEMOS_API_KEY environment variable not set")
            base_url = os.getenv("EVERMEMOS_BASE_URL")
            kwargs: dict = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            self._client = EverMemOS(**kwargs)
        return self._client

    # --- 存入 ---

    async def _add_one(
        self, agent_name: str, message_id: str, content: str, flush: bool = True
    ) -> bool:
        """向 EverMemOS 写入单条事件（所有写入路径的统一入口）。"""
        try:
            response = self._get_client().v0.memories.add(
                message_id=message_id,
                create_time=datetime.utcnow().isoformat() + "Z",
                sender=agent_name,
                sender_name=agent_name,
                group_id=agent_name,
                content=content,
                flush=flush,
            )
            if response.status != "success":
                routing_logger.warning(
                    f"[EverMemOS] {agent_name} {message_id} 写入失败: {response.message}"
                )
                return False
            routing_logger.info(f"[EverMemOS] {agent_name} {message_id} 写入成功")
            return True
        except Exception as e:
            routing_logger.error(f"[EverMemOS] {agent_name} {message_id} 写入异常: {e}")
            return False

    def schedule_add(self, agent_name: str, message_id: str, content: str):
        """非阻塞：将单条事件写入丢到后台执行。"""
        task = asyncio.create_task(self._add_one(agent_name, message_id, content))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def schedule_add_round(self, targets: list[str], round_id: str, content: str):
        """每轮对话结束后调用：为每个参与角色后台写入该轮原始对话记录。

        使用 group_id=agent_name 隔离，搜索时各角色只能找到自己参与的轮次。
        """
        for agent_name in targets:
            self.schedule_add(agent_name, f"{agent_name}_{round_id}", content)

    async def rebuild(self, agent_name: str):
        """全量重建：从 memory.md 读取，按事件分割后写入 EverMemOS。

        用于向量库数据异常时的修复操作。
        注意：EverMemOS 目前不提供删除 API，重建通过写入新版本实现
        （相同 query 召回多条时按时间排序取最新）。
        """
        from memory.text_utils import normalize, split_by_date, split_into_events

        path = Path(character_path(agent_name, "memory.md"))
        if not path.exists():
            routing_logger.warning(
                f"[EverMemOS] {agent_name} memory.md 不存在，跳过重建"
            )
            return

        content = normalize(path.read_text(encoding="utf-8"))
        sections = split_by_date(content)
        if not sections:
            routing_logger.warning(
                f"[EverMemOS] {agent_name} memory.md 无有效日期段，跳过重建"
            )
            return

        total = 0
        for date, day_content in sections.items():
            for idx, event in enumerate(split_into_events(day_content)):
                message_id = f"{agent_name}_{date}_{idx}"
                ok = await self._add_one(agent_name, message_id, event)
                if ok:
                    total += 1

        routing_logger.info(f"[EverMemOS] {agent_name} 全量重建完成: {total} 个事件")

    # --- 搜索 ---

    def search_sync(
        self, agent_name: str, query: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """同步语义搜索，供 Agno instructions 使用。"""
        try:
            response = self._get_client().v0.memories.search(
                extra_query={"user_id": agent_name, "query": query, "top_k": limit}
            )
            results = []
            if response.result and response.result.memories:
                for mem in response.result.memories:
                    # SDK 返回 pydantic model；episode 优先，其次 summary
                    content = getattr(mem, "episode", "") or getattr(mem, "summary", "")
                    results.append(
                        {
                            "id": getattr(mem, "id", ""),
                            "content": content,
                            "distance": getattr(mem, "score", 0.0) or 0.0,
                        }
                    )
            routing_logger.info(
                f"[EverMemOS] {agent_name} 搜索 '{query[:30]}...' 召回 {len(results)} 条"
            )
            return results
        except Exception as e:
            routing_logger.error(f"[EverMemOS] {agent_name} 搜索失败: {e}")
            return []

    # --- 删除 ---

    async def delete(self, agent_name: str) -> bool:
        """删除 EverMemOS 中指定角色的所有记忆（group_id=agent_name）。"""
        try:
            response = self._get_client().v0.memories.delete(
                extra_body={"user_id": agent_name, "group_id": agent_name}
            )
            success = getattr(response, "status", "") == "ok"
            if success:
                routing_logger.info(f"[EverMemOS] {agent_name} 记忆删除成功")
            else:
                routing_logger.warning(
                    f"[EverMemOS] {agent_name} 记忆删除失败: {getattr(response, 'message', 'unknown')}"
                )
            return success
        except Exception as e:
            routing_logger.error(f"[EverMemOS] {agent_name} 删除记忆失败: {e}")
            return False

    async def delete_all_agents(self, agent_names: list[str]) -> dict[str, bool]:
        """并行删除多个角色的记忆。"""
        results = await asyncio.gather(*(self.delete(name) for name in agent_names))
        return dict(zip(agent_names, results))

    async def close(self):
        """等待后台任务完成"""
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)


# 全局实例
vector_store = VectorStore()
