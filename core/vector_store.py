"""向量存储 - 使用 sqlite-vec"""

import os
import json
import hashlib
import sqlite_vec
import aiosqlite
import httpx
from datetime import datetime
from typing import Any


# 获取 sqlite-vec 扩展的路径
_SQLITE_VEC_PATH = sqlite_vec.__file__.replace("__init__.py", "vec0")

# Embedding 配置（从环境变量读取，有默认值）
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "openai/text-embedding-3-small")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1536"))
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")


class VectorStore:
    """基于 sqlite-vec 的向量存储 - 文件级变更追踪"""

    def __init__(self, db_path: str = "data/memory.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

    async def _get_db(self):
        """获取数据库连接"""
        db = await aiosqlite.connect(self.db_path)
        await db.enable_load_extension(True)
        await db.execute(f"SELECT load_extension('{_SQLITE_VEC_PATH}')")
        return db

    async def init_tables(self):
        """初始化数据库表"""
        db = await self._get_db()
        try:
            # 表1: 文件跟踪表 - 检测文件是否变更
            await db.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(agent_name, file_path)
                )
            """)

            # 表2: 向量表 - 存储 chunks 和 embeddings
            await db.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING vec0(
                    id INTEGER PRIMARY KEY,
                    agent_name TEXT,
                    source_file TEXT,
                    chunk_index INTEGER,
                    content TEXT,
                    created_at TEXT,
                    embedding FLOAT[1536]
                )
            """)
            await db.commit()
        finally:
            await db.close()

    def _compute_hash(self, content: str) -> str:
        """计算内容哈希"""
        return hashlib.sha256(content.encode()).hexdigest()

    async def _get_embedding(self, text: str) -> list[float]:
        """获取文本的 embedding 向量"""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/embeddings",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": EMBEDDING_MODEL,
                    "input": text,
                },
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()
            return data["data"][0]["embedding"]

    def _mock_embedding(self, text: str, dim: int = EMBEDDING_DIM) -> list[float]:
        """模拟 embedding 生成（备用方案）"""
        hash_bytes = hashlib.sha256(text.encode()).digest()
        vector = []
        for i in range(dim):
            val = (hash_bytes[i % 32] / 128.0) - 1.0
            vector.append(val)
        return vector

    def _chunk_text(self, text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
        """将文本分块"""
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            # 尽量在句子边界切割
            if end < len(text):
                # 找最近的句号、问号、感叹号或换行
                for sep in ['。', '？', '！', '\n', '. ', '? ', '! ']:
                    pos = text.rfind(sep, start, end)
                    if pos != -1:
                        end = pos + 1
                        break
            chunks.append(text[start:end].strip())
            start = end - overlap if end < len(text) else end
        return chunks

    async def sync_file(
        self,
        agent_name: str,
        file_path: str,
        content: str,
    ) -> tuple[bool, str]:
        """
        同步文件到向量库

        Returns:
            (是否发生变更, 消息)
        """
        content_hash = self._compute_hash(content)
        db = await self._get_db()

        try:
            # 检查文件是否已存在且未变更
            cursor = await db.execute(
                "SELECT content_hash FROM files WHERE agent_name = ? AND file_path = ?",
                (agent_name, file_path)
            )
            row = await cursor.fetchone()

            if row and row[0] == content_hash:
                return False, f"文件未变更: {file_path}"

            # 文件是新文件或已变更，删除旧的 chunks
            await db.execute(
                "DELETE FROM chunks WHERE agent_name = ? AND source_file = ?",
                (agent_name, file_path)
            )

            # 分块并生成 embeddings
            chunks = self._chunk_text(content)
            timestamp = datetime.now().isoformat()

            for idx, chunk_content in enumerate(chunks):
                if not chunk_content.strip():
                    continue
                embedding = await self._get_embedding(chunk_content)
                await db.execute(
                    """
                    INSERT INTO chunks (agent_name, source_file, chunk_index, content, created_at, embedding)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (agent_name, file_path, idx, chunk_content, timestamp, json.dumps(embedding))
                )

            # 更新文件记录
            if row:
                await db.execute(
                    """
                    UPDATE files SET content_hash = ?, updated_at = ?
                    WHERE agent_name = ? AND file_path = ?
                    """,
                    (content_hash, timestamp, agent_name, file_path)
                )
            else:
                await db.execute(
                    """
                    INSERT INTO files (agent_name, file_path, content_hash, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (agent_name, file_path, content_hash, timestamp)
                )

            await db.commit()
            return True, f"已同步 {len(chunks)} 个 chunks: {file_path}"

        finally:
            await db.close()

    async def search(
        self,
        agent_name: str,
        query: str,
        query_embedding: list[float] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """搜索记忆"""
        if query_embedding is None:
            query_embedding = await self._get_embedding(query)

        db = await self._get_db()
        try:
            cursor = await db.execute(
                """
                SELECT id, source_file, chunk_index, content, created_at, distance
                FROM chunks
                WHERE agent_name = ? AND embedding MATCH ?
                ORDER BY distance ASC
                LIMIT ?
                """,
                (agent_name, json.dumps(query_embedding), limit),
            )
            rows = await cursor.fetchall()

            return [
                {
                    "id": row[0],
                    "source_file": row[1],
                    "chunk_index": row[2],
                    "content": row[3],
                    "created_at": row[4],
                    "distance": row[5],
                }
                for row in rows
            ]
        finally:
            await db.close()

    async def delete_agent_files(self, agent_name: str):
        """删除角色的所有文件记录和 chunks"""
        db = await self._get_db()
        try:
            await db.execute("DELETE FROM chunks WHERE agent_name = ?", (agent_name,))
            await db.execute("DELETE FROM files WHERE agent_name = ?", (agent_name,))
            await db.commit()
        finally:
            await db.close()

    async def get_file_status(self, agent_name: str) -> list[dict[str, Any]]:
        """获取角色的文件同步状态"""
        db = await self._get_db()
        try:
            cursor = await db.execute(
                "SELECT file_path, content_hash, updated_at FROM files WHERE agent_name = ?",
                (agent_name,)
            )
            rows = await cursor.fetchall()
            return [
                {
                    "file_path": row[0],
                    "content_hash": row[1][:16] + "...",
                    "updated_at": row[2],
                }
                for row in rows
            ]
        finally:
            await db.close()


# 全局实例
vector_store = VectorStore()
