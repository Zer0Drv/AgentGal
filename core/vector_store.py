"""向量存储 - 使用 sqlite-vec"""

import os
import json
import aiosqlite
from datetime import datetime
from typing import List, Dict, Any, Optional


class VectorStore:
    """基于 sqlite-vec 的向量存储"""

    def __init__(self, db_path: str = "data/memory.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

    async def _get_db(self):
        """获取数据库连接"""
        db = await aiosqlite.connect(self.db_path)
        await db.enable_load_extension(True)
        try:
            await db.load_extension("vec0")
        except Exception:
            # 某些环境可能需要不同的加载方式
            pass
        return db

    async def init_agent_table(self, agent_name: str):
        """为角色初始化向量表"""
        table_name = f"{agent_name}_memories"
        async with await self._get_db() as db:
            # 创建虚拟表用于向量搜索
            await db.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {table_name} USING vec0(
                    id INTEGER PRIMARY KEY,
                    content TEXT,
                    source TEXT,
                    created_at TEXT,
                    embedding FLOAT[1536]
                )
            """)
            await db.commit()

    async def add_memory(
        self,
        agent_name: str,
        content: str,
        source: str = "memory.md",
        embedding: Optional[List[float]] = None,
    ):
        """添加记忆到向量库"""
        table_name = f"{agent_name}_memories"

        # 如果没有提供 embedding，使用简单的哈希模拟（实际应该用 OpenAI embedding）
        if embedding is None:
            embedding = self._mock_embedding(content)

        async with await self._get_db() as db:
            await db.execute(
                f"""
                INSERT INTO {table_name} (content, source, created_at, embedding)
                VALUES (?, ?, ?, ?)
                """,
                (
                    content,
                    source,
                    datetime.now().isoformat(),
                    json.dumps(embedding),
                ),
            )
            await db.commit()

    async def search(
        self,
        agent_name: str,
        query: str,
        query_embedding: Optional[List[float]] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """搜索记忆"""
        table_name = f"{agent_name}_memories"

        if query_embedding is None:
            query_embedding = self._mock_embedding(query)

        async with await self._get_db() as db:
            # sqlite-vec 支持向量相似度搜索
            cursor = await db.execute(
                f"""
                SELECT id, content, source, created_at,
                       vec_distance_euclidean(embedding, ?) as distance
                FROM {table_name}
                ORDER BY distance ASC
                LIMIT ?
                """,
                (json.dumps(query_embedding), limit),
            )
            rows = await cursor.fetchall()

            return [
                {
                    "id": row[0],
                    "content": row[1],
                    "source": row[2],
                    "created_at": row[3],
                    "distance": row[4],
                }
                for row in rows
            ]

    async def delete_by_source(self, agent_name: str, source: str):
        """删除特定来源的记忆（用于更新时清理旧内容）"""
        table_name = f"{agent_name}_memories"
        async with await self._get_db() as db:
            await db.execute(
                f"DELETE FROM {table_name} WHERE source = ?",
                (source,),
            )
            await db.commit()

    def _mock_embedding(self, text: str, dim: int = 1536) -> List[float]:
        """
        模拟 embedding 生成
        TODO: 实际使用时应调用 OpenAI API 获取真实 embedding
        """
        import hashlib

        # 使用文本哈希生成确定性向量
        hash_bytes = hashlib.sha256(text.encode()).digest()
        # 生成 dim 维的向量
        vector = []
        for i in range(dim):
            # 使用哈希字节生成 -1 到 1 之间的值
            val = (hash_bytes[i % 32] / 128.0) - 1.0
            vector.append(val)
        return vector


# 全局实例
vector_store = VectorStore()
