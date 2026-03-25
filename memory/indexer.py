"""记忆向量索引重建。

将 memory.md 的结构化记忆块写入向量库。
业务逻辑（解析、过滤、元数据提取）在此层，storage 层只做 I/O。
"""

from pathlib import Path

from log_config.memory import memory_logger as logger
from memory.parser import (
    canonical_cn_date,
    extract_event_field,
    is_structured_memory_block,
    normalize,
    parse_event_importance,
    split_by_date,
    split_into_events,
)
from shared.config import character_path, get_agent_names
from storage.vector_store import vector_store


async def rebuild_memory_index(agent_name: str | None = None) -> None:
    """从 memory.md 重建向量索引。

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
        path = Path(character_path(agent, "memory.md"))
        if not path.exists():
            logger.info("[indexer] 跳过 %s：未找到 memory.md", agent)
            continue

        sections = split_by_date(normalize(path.read_text(encoding="utf-8")))
        for date, content in sections.items():
            normalized_date = canonical_cn_date(date)
            if not normalized_date:
                continue
            events = [e for e in split_into_events(content) if is_structured_memory_block(e)]
            if not events:
                continue
            chunks = [
                (text, extract_event_field(text, "关键词"), parse_event_importance(text, default=3))
                for text in events
            ]
            await vector_store.add(agent, normalized_date, chunks)

        logger.info("[indexer] %s 索引完成", agent)
