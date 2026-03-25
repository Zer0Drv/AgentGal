#!/usr/bin/env python3
"""向量库重建脚本 - 重建角色的长期记忆向量数据库

Usage:
    uv run python scripts/rebuild_vectors.py             # 重建所有角色
    uv run python scripts/rebuild_vectors.py chenxiao   # 重建指定角色
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

from memory.vector_store import vector_store


def get_all_agents() -> list[str]:
    """获取 data/characters 目录下所有拥有 memory.md 的角色名称（排除 narrator）。"""
    agents_dir = project_root / "data" / "characters"
    if not agents_dir.exists():
        print(f"[错误] data/characters 目录不存在: {agents_dir}")
        return []

    agents = []
    for item in agents_dir.iterdir():
        if item.is_dir() and item.name != "narrator":
            memory_file = item / "memory.md"
            if memory_file.exists():
                agents.append(item.name)

    return sorted(agents)


async def main() -> None:
    if len(sys.argv) > 1:
        agents = [sys.argv[1]]
    else:
        agents = get_all_agents()

    if not agents:
        print("[错误] 没有找到可重建的角色")
        sys.exit(1)

    print(f"[向量重建] 开始重建: {', '.join(agents)}")
    for agent in agents:
        print(f"  → {agent} ...", end=" ", flush=True)
        try:
            await vector_store.rebuild(agent)
            print("完成")
        except Exception as e:
            print(f"失败: {e}")
            raise
    print("[向量重建] 全部完成")


if __name__ == "__main__":
    asyncio.run(main())
