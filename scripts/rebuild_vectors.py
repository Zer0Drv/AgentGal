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
            memory_file = item / "memory.md"
            if memory_file.exists():
                agents.append(item.name)

    return sorted(agents)


async def rebuild_all_vectors():
    """重建向量库（从 narrator/raw 回放一次即可）"""
    routing_logger.info("[向量重建] 开始重建向量库（从 narrator raw 回放）")
    await vector_store.rebuild("narrator")
    routing_logger.info("[向量重建] 向量库重建完成")


async def main():
    """主函数：重建所有角色的向量库"""
    try:
        await rebuild_all_vectors()
    except Exception as e:
        routing_logger.error(f"[向量重建] 执行失败: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
