"""后台记忆整理器 - 定期对 memory.md 进行去重归纳"""

import os
import asyncio
import httpx
from .routing_logger import routing_logger

# 整理间隔（每多少轮对话触发一次）
CONSOLIDATION_INTERVAL = int(os.getenv("CONSOLIDATION_INTERVAL", "10"))

CONSOLIDATION_PROMPT = """\
你是一个记忆整理助手。你的任务是对以下角色的长期记忆进行**去重归纳**。

## 规则

1. **保留所有细节**：不要做摘要压缩，不要丢失任何事实、对话要点、情感变化
2. **合并重复条目**：如果同一事件被多次描述，合并为一条，保留最完整的版本
3. **按时间顺序排列**：从最早到最近，保持时间线清晰
4. **保持原有格式**：使用 Markdown，按时间段分组（如"4月上旬"），用标题和列表组织
5. **不要添加任何新内容**：只整理已有内容，不推测、不补充
6. **保持角色视角**：这是 {agent_name} 的记忆，保持其主观视角

## 当前记忆内容

{memory_content}

## 输出

请输出整理后的完整记忆内容（纯 Markdown，不要包含任何解释说明）：
"""


class MemoryConsolidator:
    """后台记忆整理器"""

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        self._client: httpx.AsyncClient | None = None

    def _get_lock(self, agent_name: str) -> asyncio.Lock:
        """获取角色专属锁，防止同一角色并发整理"""
        if agent_name not in self._locks:
            self._locks[agent_name] = asyncio.Lock()
        return self._locks[agent_name]

    async def _get_http_client(self) -> httpx.AsyncClient:
        """获取或创建 HTTP 客户端"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=120.0,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._client

    async def _call_llm(self, prompt: str) -> str:
        """直接调用 OpenRouter chat API"""
        api_key = os.getenv("OPENROUTER_API_KEY")
        model_id = os.getenv("MODEL_ID", "anthropic/claude-3.5-sonnet")

        client = await self._get_http_client()
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    async def consolidate_agent(self, agent_name: str):
        """整理单个角色的 memory.md"""
        lock = self._get_lock(agent_name)

        # 如果已有整理任务在跑，跳过
        if lock.locked():
            routing_logger.info(f"[整理器] {agent_name} 已有整理任务在运行，跳过")
            return

        async with lock:
            memory_path = f"agents/{agent_name}/memory/Memory.md"

            if not os.path.exists(memory_path):
                routing_logger.info(f"[整理器] {agent_name} 无 Memory.md，跳过")
                return

            with open(memory_path, "r", encoding="utf-8") as f:
                current_memory = f.read()

            # 内容太短不需要整理
            if len(current_memory.strip()) < 200:
                routing_logger.info(f"[整理器] {agent_name} 记忆内容过短，跳过整理")
                return

            routing_logger.info(f"[整理器] 开始整理 {agent_name} 的记忆 (长度: {len(current_memory)})")

            try:
                prompt = CONSOLIDATION_PROMPT.format(
                    agent_name=agent_name,
                    memory_content=current_memory,
                )
                consolidated = await self._call_llm(prompt)

                # 写回文件
                with open(memory_path, "w", encoding="utf-8") as f:
                    f.write(consolidated)

                routing_logger.info(f"[整理器] {agent_name} 记忆整理完成 (长度: {len(current_memory)} → {len(consolidated)})")

            except Exception as e:
                routing_logger.info(f"[整理器] {agent_name} 记忆整理失败: {e}")

    async def consolidate_all(self, agent_names: list[str]):
        """并行整理所有角色的记忆"""
        routing_logger.info(f"[整理器] 开始后台记忆整理: {agent_names}")
        tasks = [self.consolidate_agent(name) for name in agent_names]
        await asyncio.gather(*tasks, return_exceptions=True)
        routing_logger.info("[整理器] 后台记忆整理完成")

    async def close(self):
        """关闭 HTTP 客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# 全局实例
memory_consolidator = MemoryConsolidator()

