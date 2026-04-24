"""记忆向量索引重建。

将 memory.jsonl 的结构化记忆记录写入向量库。
"""

from log_config.memory import memory_logger as logger
from memory.parser import (
    memory_jsonl_path,
    read_memory_jsonl,
)
from shared.config import get_agent_names
from storage.vector_store import vector_store


async def rebuild_memory_index(agent_name: str | None = None) -> None:
    """从 memory.jsonl 重建向量索引。

    Args:
        agent_name: 指定角色时只重建该角色；为 None 时重建所有非 narrator 角色。
    """
    if agent_name is not None:
        agents = [agent_name]
        await vector_store.delete(agent_name)
    else:
        try:
            agents = get_agent_names(include_narrator=False)
        except TypeError:
            agents = [a for a in get_agent_names() if a != "narrator"]
        await vector_store.delete_all_agents(agents)

    for agent in agents:
        if not memory_jsonl_path(agent).exists():
            logger.info("[indexer] 跳过 %s：未找到 memory.jsonl", agent)
            continue

        for record in read_memory_jsonl(agent):
            await vector_store.add(record)

        logger.info("[indexer] %s 索引完成", agent)
