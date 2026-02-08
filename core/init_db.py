"""数据库初始化 - 确保所有角色的向量表存在"""

import asyncio
from .vector_store import vector_store


async def init_database():
    """初始化所有角色的数据库表"""
    agents = ["lilith", "ruri", "mitsuki", "narrator"]

    print("[DB] 初始化向量数据库...")

    for agent_name in agents:
        try:
            await vector_store.init_agent_table(agent_name)
            print(f"[DB]  {agent_name}_memories 表已就绪")
        except Exception as e:
            print(f"[DB]  {agent_name}_memories 表初始化失败: {e}")
            raise

    print("[DB] 数据库初始化完成")


def init_db_sync():
    """同步方式初始化数据库（用于应用启动时）"""
    asyncio.run(init_database())


if __name__ == "__main__":
    # 直接运行此文件进行初始化
    asyncio.run(init_database())
