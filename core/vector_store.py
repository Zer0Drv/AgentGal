"""向量存储 - 增量 embedding + 后台任务，不阻塞 tool 执行"""

import os
import json
import hashlib
import asyncio
import sqlite_vec
import aiosqlite
import httpx
from typing import Any
from .routing_logger import routing_logger

_SQLITE_VEC_PATH = sqlite_vec.__file__.replace("__init__.py", "vec0")

EMBEDDING_API_URL = os.getenv("EMBEDDING_API_URL", "https://api.openai.com/v1/embeddings")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1536"))

DB_PATH = "data/memory.db"


class VectorStore:
    """增量 embedding 向量存储，写入后台化，搜索即时返回"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._client: httpx.AsyncClient | None = None
        self._bg_tasks: set[asyncio.Task] = set()
        self._initialized = False

    async def _get_http_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._client

    async def _get_db(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self.db_path)
        await db.enable_load_extension(True)
        await db.execute(f"SELECT load_extension('{_SQLITE_VEC_PATH}')")
        return db

    async def init_tables(self):
        """初始化数据库表（幂等）"""
        if self._initialized:
            return
        db = await self._get_db()
        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sync_state (
                    agent_name TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL DEFAULT '',
                    sync_offset INTEGER NOT NULL DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING vec0(
                    id INTEGER PRIMARY KEY,
                    agent_name TEXT,
                    chunk_index INTEGER,
                    content TEXT,
                    embedding FLOAT[{dim}]
                )
            """.format(dim=EMBEDDING_DIM))
            await db.commit()
            self._initialized = True
        finally:
            await db.close()

    async def _get_embeddings(self, texts: list[str]) -> list[list[float]]:
        """批量获取 embedding，一次 API 调用处理多个文本"""
        api_key = os.getenv("EMBEDDING_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
        client = await self._get_http_client()
        resp = await client.post(
            EMBEDDING_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"model": EMBEDDING_MODEL, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        # API 返回的顺序按 index 排列，确保对齐
        data.sort(key=lambda x: x["index"])
        return [item["embedding"] for item in data]

    async def _get_embedding(self, text: str) -> list[float]:
        """单条 embedding（搜索时用）"""
        results = await self._get_embeddings([text])
        return results[0]

    def _chunk_text(self, text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            if end < len(text):
                for sep in ["。", "？", "！", "\n", ". ", "? ", "! "]:
                    pos = text.rfind(sep, start, end)
                    if pos != -1:
                        end = pos + 1
                        break
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            start = end - overlap if end < len(text) else end
        return chunks

    # --- 增量 embedding（后台执行） ---

    async def _sync_incremental(self, agent_name: str, content: str) -> bool:
        """增量同步：只对 sync_offset 之后的新内容做 embedding

        Returns:
            True 表示同步成功，False 表示失败
        """
        await self.init_tables()
        db = await self._get_db()
        try:
            cursor = await db.execute(
                "SELECT sync_offset FROM sync_state WHERE agent_name = ?",
                (agent_name,),
            )
            row = await cursor.fetchone()
            offset = row[0] if row else 0

            new_text = content[offset:]
            if not new_text.strip():
                return True

            chunks = self._chunk_text(new_text)
            if not chunks:
                return True

            # 获取当前最大 chunk_index
            cursor = await db.execute(
                "SELECT MAX(chunk_index) FROM chunks WHERE agent_name = ?",
                (agent_name,),
            )
            row = await cursor.fetchone()
            start_idx = (row[0] or -1) + 1

            # 批量 embedding：一次 API 调用处理所有 chunks
            BATCH_SIZE = 32
            for batch_start in range(0, len(chunks), BATCH_SIZE):
                batch = chunks[batch_start:batch_start + BATCH_SIZE]
                embeddings = await self._get_embeddings(batch)
                for i, (chunk_content, embedding) in enumerate(zip(batch, embeddings)):
                    idx = start_idx + batch_start + i
                    await db.execute(
                        "INSERT INTO chunks (agent_name, chunk_index, content, embedding) "
                        "VALUES (?, ?, ?, ?)",
                        (agent_name, idx, chunk_content, json.dumps(embedding)),
                    )

            # 更新 sync_offset
            new_offset = len(content)
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            if offset == 0:
                await db.execute(
                    "INSERT OR REPLACE INTO sync_state (agent_name, content_hash, sync_offset) "
                    "VALUES (?, ?, ?)",
                    (agent_name, content_hash, new_offset),
                )
            else:
                await db.execute(
                    "UPDATE sync_state SET sync_offset = ?, content_hash = ? "
                    "WHERE agent_name = ?",
                    (new_offset, content_hash, agent_name),
                )
            await db.commit()
            routing_logger.info(
                f"[向量库] {agent_name} 增量同步完成: "
                f"{len(chunks)} chunks, offset {offset} → {new_offset}"
            )
            return True
        except Exception as e:
            routing_logger.error(f"[向量库] {agent_name} 增量同步失败: {e}")
            return False
        finally:
            await db.close()

    # --- 全量重建（consolidator 整理后调用） ---

    async def rebuild(self, agent_name: str, content: str):
        """全量重建：删除旧数据，重新 embedding 全部内容"""
        await self.init_tables()
        db = await self._get_db()
        try:
            await db.execute(
                "DELETE FROM chunks WHERE agent_name = ?", (agent_name,)
            )
            await db.commit()
            # 重置 offset 为 0，然后走增量逻辑
            await db.execute(
                "INSERT OR REPLACE INTO sync_state (agent_name, content_hash, sync_offset) "
                "VALUES (?, '', 0)",
                (agent_name,),
            )
            await db.commit()
        finally:
            await db.close()

        ok = await self._sync_incremental(agent_name, content)
        if ok:
            routing_logger.info(f"[向量库] {agent_name} 全量重建完成")
        else:
            routing_logger.error(f"[向量库] {agent_name} 全量重建失败（增量同步出错）")

    # --- 搜索（即时返回，查已有索引） ---

    async def search(
        self, agent_name: str, query: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """语义搜索，直接查已有索引，不等待后台任务"""
        await self.init_tables()
        query_embedding = await self._get_embedding(query)
        db = await self._get_db()
        try:
            cursor = await db.execute(
                """
                SELECT id, chunk_index, content, distance
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
                    "chunk_index": row[1],
                    "content": row[2],
                    "distance": row[3],
                }
                for row in rows
            ]
        finally:
            await db.close()

    # --- 查询与调度 ---

    async def sync_if_needed(self, agent_names: list[str]):
        """检查各 agent 的 memory.md，offset 落后则后台补做 embedding"""
        await self.init_tables()
        for name in agent_names:
            path = f"agents/{name}/memory/Memory.md"
            if not os.path.exists(path):
                continue
            content = open(path, "r", encoding="utf-8").read()
            file_len = len(content)
            db = await self._get_db()
            try:
                cursor = await db.execute(
                    "SELECT sync_offset FROM sync_state WHERE agent_name = ?",
                    (name,),
                )
                row = await cursor.fetchone()
                offset = row[0] if row else 0
            finally:
                await db.close()
            if offset < file_len:
                routing_logger.info(
                    f"[启动] {name} 记忆未完全同步 "
                    f"(offset={offset}, file_len={file_len})，触发后台同步"
                )
                self.schedule_incremental_sync(name, content)

    def schedule_incremental_sync(self, agent_name: str, content: str):
        """非阻塞：将增量 embedding 丢到后台执行"""
        task = asyncio.create_task(self._sync_incremental(agent_name, content))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def delete_agent(self, agent_name: str):
        """删除角色的所有向量数据（重置游戏时用）"""
        await self.init_tables()
        db = await self._get_db()
        try:
            await db.execute(
                "DELETE FROM chunks WHERE agent_name = ?", (agent_name,)
            )
            await db.execute(
                "DELETE FROM sync_state WHERE agent_name = ?", (agent_name,)
            )
            await db.commit()
        finally:
            await db.close()

    async def close(self):
        """等待后台任务完成并关闭 HTTP 客户端"""
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# 全局实例
vector_store = VectorStore()

