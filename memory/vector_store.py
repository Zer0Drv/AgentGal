"""向量存储 - EverMemOS 后端，本地保留 memory.md 备份"""

import os
import asyncio
from datetime import datetime
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
    """EverMemOS 向量存储，本地 memory.md 作为人类可读备份"""

    def __init__(self):
        self._client = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._initialized = False

    def _get_client(self):
        """获取 EverMemOS 客户端（延迟初始化）"""
        if self._client is None:
            if not _HAS_EVERMEMOS:
                raise ImportError(
                    "evermemos SDK not installed. "
                    "Run: uv add evermemos"
                )
            api_key = os.getenv("EVERMEMOS_API_KEY")
            if not api_key:
                raise ValueError("EVERMEMOS_API_KEY environment variable not set")
            base_url = os.getenv("EVERMEMOS_BASE_URL")
            kwargs = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            self._client = EverMemOS(**kwargs)
        return self._client

    def _chunk_text(self, text: str, chunk_size: int = 2000, overlap: int = 200) -> list[str]:
        """按事件分块，每个事件作为一个独立的 chunk。

        事件格式：
        ## X月X日
        - **时间**：...
        - **地点**：...
        - **在场**：...
        - **内容**：...
        """
        import re

        # 按日期标题分割，然后提取每个事件
        date_pattern = re.compile(r'^##\s*(\d{1,2}月\d{1,2}日|\d{4}-\d{2}-\d{2}).*$', re.MULTILINE)
        date_matches = list(date_pattern.finditer(text))

        if not date_matches:
            # 没有日期格式，兜底
            return self._chunk_text_sliding_window(text, chunk_size, overlap)

        chunks: list[str] = []

        for i, date_match in enumerate(date_matches):
            date = date_match.group(1)
            start = date_match.end()
            end = date_matches[i + 1].start() if i + 1 < len(date_matches) else len(text)
            section = text[start:end].strip()

            if not section:
                continue

            # 在日期块内按事件分割（以 - **时间**：开头的条目）
            # 事件条目通常以空行或行首的 "- **" 分隔
            event_pattern = re.compile(r'(^|\n)- \*\*时间\*\*：', re.MULTILINE)
            events = list(event_pattern.finditer(section))

            if not events:
                # 没有找到标准事件格式，整个日期块作为一个 chunk
                chunk_content = f"## {date}\n{section}"
                chunks.append(chunk_content)
                continue

            for j, event_match in enumerate(events):
                event_start = event_match.start()
                event_end = events[j + 1].start() if j + 1 < len(events) else len(section)
                event_content = section[event_start:event_end].strip()

                if event_content:
                    # 组装完整事件：日期 + 事件内容
                    chunk_content = f"## {date}\n{event_content}"
                    chunks.append(chunk_content)

        return chunks if chunks else self._chunk_text_sliding_window(text, chunk_size, overlap)

    def _chunk_text_sliding_window(self, text: str, chunk_size: int = 2000, overlap: int = 200) -> list[str]:
        """滑动窗口 + 句子边界分块（兜底策略）"""
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            if end < len(text):
                for sep in ["，", "。", "？", "！", "\n", ". ", "? ", "! "]:
                    pos = text.rfind(sep, start, end)
                    if pos != -1:
                        end = pos + 1
                        break
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            start = end - overlap if end < len(text) else end
        return chunks

    # --- 同步到 EverMemOS ---

    async def _sync_incremental(self, agent_name: str, content: str) -> bool:
        """增量同步：将新内容写入 EverMemOS

        策略：
        - memory.md 按日期分块
        - 每个 chunk 作为一个 episode 记忆
        - 使用日期作为 message_id 的一部分（幂等）
        """
        try:
            memory = self._get_client().v0.memories

            chunks = self._chunk_text(content)
            if not chunks:
                return True

            # 批量写入
            for idx, chunk in enumerate(chunks):
                # 生成 message_id: lilith_20240405_0
                today = datetime.now().strftime("%Y%m%d")
                message_id = f"{agent_name}_{today}_{idx}"

                # 最后一条标记 flush
                is_last = (idx == len(chunks) - 1)

                response = memory.add(
                    message_id=message_id,
                    create_time=datetime.utcnow().isoformat() + "Z",
                    sender=agent_name,
                    sender_name=agent_name,
                    group_id=agent_name,  # 每个 agent 一个 group
                    content=chunk,
                    flush=is_last,
                )

                if response.status != "success":
                    routing_logger.warning(
                        f"[EverMemOS] {agent_name} chunk {idx} 写入失败: {response.message}"
                    )

            routing_logger.info(
                f"[EverMemOS] {agent_name} 同步完成: {len(chunks)} chunks"
            )
            return True

        except Exception as e:
            routing_logger.error(f"[EverMemOS] {agent_name} 同步失败: {e}")
            return False

    async def rebuild(self, agent_name: str, content: str):
        """全量重建：重新索引全部内容

        注意：EverMemOS 目前不提供删除 API，
        重建通过写入新版本（相同 query 会召回多条，按时间排序取最新）
        """
        ok = await self._sync_incremental(agent_name, content)
        if ok:
            routing_logger.info(f"[EverMemOS] {agent_name} 全量重建完成")
        else:
            routing_logger.error(f"[EverMemOS] {agent_name} 全量重建失败")

    # --- 搜索 ---

    async def search(
        self, agent_name: str, query: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """语义搜索，调用 EverMemOS"""
        try:
            memory = self._get_client().v0.memories

            response = memory.search(
                extra_query={
                    "user_id": agent_name,
                    "query": query,
                    "limit": limit,
                }
            )

            results = []
            if response.result and response.result.memories:
                for mem in response.result.memories:
                    results.append({
                        "id": mem.get("message_id", ""),
                        "chunk_index": 0,
                        "content": mem.get("content", ""),
                        "distance": mem.get("score", 0.0),
                    })

            routing_logger.debug(
                f"[EverMemOS] {agent_name} 搜索 '{query[:30]}...' 召回 {len(results)} 条"
            )
            return results

        except Exception as e:
            routing_logger.error(f"[EverMemOS] {agent_name} 搜索失败: {e}")
            return []

    # --- 查询与调度 ---

    async def sync_if_needed(self, agent_names: list[str]):
        """检查各 agent 的 memory.md，触发后台同步"""
        for name in agent_names:
            path = character_path(name, "memory.md")
            if not os.path.exists(path):
                continue
            content = open(path, "r", encoding="utf-8").read()
            routing_logger.info(f"[启动] {name} 触发 EverMemOS 同步")
            self.schedule_incremental_sync(name, content)

    def schedule_incremental_sync(self, agent_name: str, content: str):
        """非阻塞：将增量同步丢到后台执行"""
        task = asyncio.create_task(self._sync_incremental(agent_name, content))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def delete_agent(self, agent_name: str):
        """删除角色的向量数据

        注意：EverMemOS 可能不提供删除 API，这里记录日志
        """
        routing_logger.warning(
            f"[EverMemOS] {agent_name} 删除操作尚未实现"
        )

    async def close(self):
        """等待后台任务完成"""
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)


# 全局实例
vector_store = VectorStore()
