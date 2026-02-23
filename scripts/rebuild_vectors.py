#!/usr/bin/env python3
"""向量库重建脚本 - 重建所有角色的向量数据库

Usage:
    uv run python scripts/rebuild_vectors.py
"""

import asyncio
import sys
from pathlib import Path

# 将项目根目录加入路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 必须先加载环境变量，再导入 vector_store
from dotenv import load_dotenv
load_dotenv(project_root / ".env")

from log_config.routing import routing_logger
from memory.vector_store import vector_store


def get_all_agents() -> list[str]:
    """获取 data/characters 目录下所有角色名称"""
    agents_dir = project_root / "data" / "characters"
    if not agents_dir.exists():
        routing_logger.error(f"data/characters 目录不存在: {agents_dir}")
        return []

    agents = []
    for item in agents_dir.iterdir():
        if item.is_dir():
            memory_file = item / "Memory.md"
            if memory_file.exists():
                agents.append(item.name)

    return sorted(agents)


async def rebuild_agent_vectors(agent_name: str):
    """重建单个角色的向量库"""
    memory_path = project_root / "data" / "characters" / agent_name / "Memory.md"

    if not memory_path.exists():
        routing_logger.warning(f"[向量重建] {agent_name} 的 Memory.md 不存在，跳过")
        return

    content = memory_path.read_text(encoding="utf-8")
    if not content.strip():
        routing_logger.warning(f"[向量重建] {agent_name} 的 Memory.md 为空，跳过")
        return

    routing_logger.info(f"[向量重建] 开始重建 {agent_name} 的向量库 ({len(content)} 字符)")
    await vector_store.rebuild(agent_name, content)
    routing_logger.info(f"[向量重建] {agent_name} 重建完成")


async def main():
    """主函数：重建所有角色的向量库"""
    agents = get_all_agents()
    if not agents:
        routing_logger.error("未找到任何角色")
        return

    routing_logger.info(f"[向量重建] 发现 {len(agents)} 个角色: {agents}")

    try:
        # 并行重建所有角色的向量库
        await asyncio.gather(
            *(rebuild_agent_vectors(name) for name in agents),
            return_exceptions=True
        )
        routing_logger.info("[向量重建] 全部完成")
    except Exception as e:
        routing_logger.error(f"[向量重建] 执行失败: {e}")
        raise
    finally:
        await vector_store.close()


if __name__ == "__main__":
    asyncio.run(main())
