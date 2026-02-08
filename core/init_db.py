"""数据库初始化 - 确保所有角色的向量表存在"""

import asyncio
import os
from .vector_store import vector_store


async def init_database():
    """初始化向量数据库（每次启动清空）"""
    print("[DB] 初始化向量数据库...")

    # 删除已有数据库文件
    if os.path.exists(vector_store.db_path):
        os.remove(vector_store.db_path)
        print(f"[DB] 已删除旧数据库: {vector_store.db_path}")

    try:
        await vector_store.init_tables()
        print("[DB]  files 和 chunks 表已就绪")
    except Exception as e:
        print(f"[DB]  表初始化失败: {e}")
        raise

    print("[DB] 数据库初始化完成")


def init_db_sync():
    """同步方式初始化数据库（用于应用启动时）"""
    asyncio.run(init_database())


if __name__ == "__main__":
    # 直接运行此文件进行初始化
    asyncio.run(init_database())
