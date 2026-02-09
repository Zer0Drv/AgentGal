"""后台记忆整理器 - 定期对 memory.md 进行去重归纳"""

import os
import asyncio
import httpx
from .routing_logger import routing_logger

# 整理间隔（每多少轮对话触发一次）
CONSOLIDATION_INTERVAL = int(os.getenv("CONSOLIDATION_INTERVAL", "10"))

CONSOLIDATION_PROMPT = """\
你是一个记忆整理助手。你的任务是对以下角色的长期记忆进行**场景化归纳**，提升信息密度。

## 核心原则

以**场景**为最小单位组织记忆，而非以分钟为单位。同一地点、同一段连续互动应合并为一条记忆。

## 规则

1. **按场景合并**：同一地点的连续互动合并为一条。判断标准：地点相同 + 时间连续 + 话题连贯。例如"教室收拾书包时的两段对话"应合为一条。
2. **提炼关键信息**：每条记忆只保留——关键对话要点、情感转折、重要决策/行为、关系变化。**删除大部分环境描写**（天气、光线、声音、气味等），只保留关键的，记忆的重点在于事件和情感。
3. **时间粒度放粗**：用"放学后"、"傍晚"、"午休"等自然时段描述，不要精确到分钟。可用"场景标签"代替时间戳，如「放学后 · 教室→商店街」。
4. **保留事实，压缩修辞**：不丢失任何事实、承诺、情感变化，但删除冗余的氛围描写和重复信息。
5. **不要添加新内容**：只整理已有内容，不推测、不补充。
6. **保持角色视角**：这是 {agent_name} 的记忆，保持其主观视角。
7. **按时间顺序排列**：从最早到最近，保持时间线清晰。

## 格式要求

```markdown
## 日期（如：4月5日）

**时段 · 场景流转**
- 合并后的记忆条目（一段连续互动 = 一条）
- ...
```

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
        """直接调用 DeepSeek chat API"""
        api_key = os.getenv("DEEPSEEK_API_KEY")
        model_id = os.getenv("MODEL_ID", "deepseek-chat")

        client = await self._get_http_client()
        response = await client.post(
            "https://api.deepseek.com/chat/completions",
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

