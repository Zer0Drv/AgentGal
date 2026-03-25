"""本地向量存储（sqlite-vec）。

仅持久化 memory 层的长期记忆事件，提供原始候选检索接口。
Pipeline 逻辑（融合/rerank/recency）由 retrieval.py 负责。
"""

from __future__ import annotations

import os
import json
import asyncio
import sqlite3
import hashlib
import logging
import re
from pathlib import Path
from typing import Any

import aiosqlite

from log_config.memory import memory_logger as routing_logger
from llm.embedding import embed_async, embed_sync, EMBED_DIM, EMBED_API_URL, EMBED_API_KEY
from shared.config import (
    character_path,
    PROJECT_ROOT,
    get_agent_names,
)

_CN_DATE_RE = re.compile(r"^\d{1,2}月\d{1,2}日$")


# ----------------------------- 配置与常量 -----------------------------

DB_PATH = str(PROJECT_ROOT / "data" / "vectors.sqlite")


# FTS 只保留中英文和数字，其他标点交给 jieba 切分后丢弃
_FTS_ALLOWED_TOKEN_RE = re.compile(r"[0-9a-z\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]+")


def _get_jieba():
    try:
        import jieba
    except ModuleNotFoundError as exc:
        raise RuntimeError("未安装 jieba，无法执行中文 FTS 分词；请先同步项目依赖") from exc

    try:
        jieba.setLogLevel(logging.ERROR)
    except Exception:
        pass
    return jieba


def _segment_fts_terms(text: str) -> list[str]:
    """使用 jieba 预分词，输出可直接写入/查询 FTS5 的 token 列表。"""
    if not text or not text.strip():
        return []

    jieba = _get_jieba()
    terms: list[str] = []
    for raw_token in jieba.cut(text, cut_all=False):
        token = raw_token.strip().lower()
        if not token:
            continue
        cleaned = "".join(_FTS_ALLOWED_TOKEN_RE.findall(token))
        if cleaned:
            terms.append(cleaned)
    return terms


def _tokenize_for_fts(text: str) -> str:
    """用 jieba 预分词后按空格拼接，交给 unicode61 按 token 建索引。"""
    return " ".join(_segment_fts_terms(text))


def _build_fts_match_query(text: str, max_terms: int = 32) -> str:
    """将自由文本转换为稳定的 FTS5 MATCH 查询。

    - 去掉 Markdown/FTS 特殊语法，避免 `*`、`:` 等触发解析错误
    - 中文使用 jieba 分词，英文/数字按连续字母数字保留
    - 使用 OR 扩大召回面，由 BM25 自行排序
    """
    terms: list[str] = []
    seen: set[str] = set()
    for token in _segment_fts_terms(text.strip()):
        if token in seen:
            continue
        seen.add(token)
        terms.append(token)
        if len(terms) >= max_terms:
            break

    if not terms:
        return ""

    return " OR ".join(f'"{term}"' for term in terms)


class VectorStore:
    """sqlite-vec 本地向量库（memory-only）。

    表结构：
    - memory_chunks(id INTEGER PK, memory_key TEXT UNIQUE, owner_agent TEXT, game_date TEXT,
                     content TEXT, keywords TEXT, importance INTEGER,
                     content_hash TEXT, last_recalled_at TEXT)
    - vec_memory_chunks USING vec0(embedding F32[EMBED_DIM])  -- rowid 对应 memory_chunks.id
    - memory_chunks_fts USING fts5(content, keywords)
    """

    def __init__(self):
        self._db: aiosqlite.Connection | None = None
        self.character_path = character_path
        self._background_tasks: set[asyncio.Task] = set()
        # 单连接下显式串行化写事务；按事件循环懒初始化避免跨 loop 复用报错
        self._write_lock: asyncio.Lock | None = None
        self._write_lock_loop: asyncio.AbstractEventLoop | None = None

    def _get_write_lock(self) -> asyncio.Lock:
        """获取当前事件循环绑定的写锁。"""
        loop = asyncio.get_running_loop()
        if self._write_lock is None or self._write_lock_loop is not loop:
            self._write_lock = asyncio.Lock()
            self._write_lock_loop = loop
        return self._write_lock

    async def _query_db_recall_by_hash(
        self,
        agent_name: str,
        date: str,
    ) -> dict[str, str]:
        """从 DB 查询 (agent, date) 现有记录的 {content_hash → last_recalled_at}。

        用于 add() 重建前保留 recall 状态；DB 为空时返回空 dict。
        """
        try:
            db = await self._get_db()
            rows = await db.execute_fetchall(
                "SELECT content_hash, last_recalled_at FROM memory_chunks "
                "WHERE owner_agent = ? AND game_date = ?",
                (agent_name, date),
            )
            return {
                str(row[0]): str(row[1])
                for row in rows
                if row[0] and row[1]
            }
        except Exception:
            return {}

    def _sidecar_recall_by_hash(
        self,
        agent_name: str,
        date: str,
    ) -> dict[str, str]:
        """从 sidecar 文件读取 {content_hash → last_recalled_at}。

        仅用于 load 后 DB 为空时的降级 fallback。
        """
        state = self._read_memory_recall_state(agent_name)
        result: dict[str, str] = {}
        for entry in state.values():
            if not isinstance(entry, dict):
                continue
            if str(entry.get("date", "")).strip() != date:
                continue
            c_hash = str(entry.get("content_hash", "")).strip()
            recalled = str(entry.get("last_recalled_at", "")).strip()
            if c_hash and recalled:
                result[c_hash] = recalled
        return result

    def _read_json_object(self, path: Path) -> dict[str, Any]:
        """读取 sidecar JSON；解析失败返回空对象。"""
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _read_memory_recall_state(self, agent_name: str) -> dict[str, dict[str, Any]]:
        """读取 recall sidecar。"""
        data = self._read_json_object(
            Path(self.character_path(agent_name, ".memory_recall_state.json"))
        )
        return {
            str(key): value
            for key, value in data.items()
            if isinstance(value, dict)
        }

    async def export_recall_state(self, agent_name: str) -> dict[str, dict[str, str]]:
        """从 DB 导出 recall 状态，供存档时写入 sidecar。

        返回格式与 _read_memory_recall_state 兼容：
        {memory_key: {"date": ..., "content_hash": ..., "last_recalled_at": ...}}
        """
        await self.init_tables()
        db = await self._get_db()
        rows = await db.execute_fetchall(
            "SELECT memory_key, game_date, content_hash, last_recalled_at "
            "FROM memory_chunks WHERE owner_agent = ?",
            (agent_name,),
        )
        state: dict[str, dict[str, str]] = {}
        for memory_key, game_date, content_hash, last_recalled_at in rows:
            if not memory_key:
                continue
            state[str(memory_key)] = {
                "date": str(game_date or "").strip(),
                "content_hash": str(content_hash or "").strip(),
                "last_recalled_at": str(last_recalled_at or game_date or "").strip(),
            }
        return state

    # ----------------------------- DB 基础 -----------------------------

    async def _load_sqlite_vec(self, conn: aiosqlite.Connection):
        """在当前连接加载 sqlite-vec 扩展（重复调用安全）。"""
        try:
            import sqlite_vec  # type: ignore

            ext_path = sqlite_vec.loadable_path()
            await conn.enable_load_extension(True)
            await conn.execute(f"SELECT load_extension('{ext_path}')")
        except Exception as e:
            raise RuntimeError(f"加载 sqlite-vec 扩展失败，请安装 sqlite_vec: {e}")

    @staticmethod
    def _load_sqlite_vec_sync(conn: sqlite3.Connection):
        """在同步 sqlite3 连接加载 sqlite-vec 扩展。"""
        try:
            import sqlite_vec  # type: ignore

            ext_path = sqlite_vec.loadable_path()
            conn.enable_load_extension(True)
            conn.execute(f"SELECT load_extension('{ext_path}')")
        except Exception as e:
            raise RuntimeError(f"加载 sqlite-vec 扩展失败: {e}")

    async def _get_db(self) -> aiosqlite.Connection:
        if self._db is None:
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
            self._db = await aiosqlite.connect(DB_PATH)
            await self._db.execute("PRAGMA journal_mode=WAL;")
            await self._load_sqlite_vec(self._db)
        return self._db

    async def _has_legacy_schema(self, db: aiosqlite.Connection) -> bool:
        """检测是否存在旧版 chunks 表。"""
        row = await (await db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='chunks'"
        )).fetchone()
        return bool(row and row[0] > 0)

    async def _drop_legacy_tables(self, db: aiosqlite.Connection) -> None:
        """删除旧版检索表。"""
        for table in ("chunks_fts", "vec_chunks", "chunks"):
            await db.execute(f"DROP TABLE IF EXISTS {table}")
        await db.execute("DROP INDEX IF EXISTS idx_chunks_round_id")
        await db.execute("DROP INDEX IF EXISTS idx_chunks_chunk_id")
        routing_logger.info("[VectorStore] 已清除旧版 schema (chunks/vec_chunks/chunks_fts)")

    async def init_tables(self):
        db = await self._get_db()

        # 旧版 schema 迁移：检测到 legacy 表则整体 drop，后续由 rebuild 重建
        if await self._has_legacy_schema(db):
            await self._drop_legacy_tables(db)
            await db.commit()

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_key TEXT NOT NULL UNIQUE,
                owner_agent TEXT NOT NULL,
                game_date TEXT NOT NULL,
                content TEXT NOT NULL,
                keywords TEXT NOT NULL DEFAULT '',
                importance INTEGER NOT NULL DEFAULT 3,
                content_hash TEXT NOT NULL,
                last_recalled_at TEXT
            )
            """
        )
        await self._ensure_memory_chunk_columns(db)
        await db.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_memory_chunks USING vec0(
                embedding F32[{EMBED_DIM}]
            )
            """
        )
        await self._ensure_memory_chunks_fts(db)
        await db.commit()

    async def _ensure_memory_chunk_columns(self, db: aiosqlite.Connection) -> None:
        rows = await (await db.execute("PRAGMA table_info(memory_chunks)")).fetchall()
        columns = {str(row[1]) for row in rows}
        if "keywords" not in columns:
            await db.execute("ALTER TABLE memory_chunks ADD COLUMN keywords TEXT NOT NULL DEFAULT ''")
        if "importance" not in columns:
            await db.execute("ALTER TABLE memory_chunks ADD COLUMN importance INTEGER NOT NULL DEFAULT 3")

    async def _ensure_memory_chunks_fts(self, db: aiosqlite.Connection) -> None:
        row = await (await db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_chunks_fts'"
        )).fetchone()
        fts_sql = str(row[0] or "") if row else ""
        if not fts_sql:
            await db.execute(
                """
                CREATE VIRTUAL TABLE memory_chunks_fts USING fts5(
                    content, keywords,
                    tokenize='unicode61'
                )
                """
            )
            await self._rebuild_fts_index(db)
            return

        normalized = " ".join(fts_sql.lower().split())
        if "keywords" not in normalized:
            await db.execute("DROP TABLE IF EXISTS memory_chunks_fts")
            await db.execute(
                """
                CREATE VIRTUAL TABLE memory_chunks_fts USING fts5(
                    content, keywords,
                    tokenize='unicode61'
                )
                """
            )
            await self._rebuild_fts_index(db)

    async def _rebuild_fts_index(self, db: aiosqlite.Connection) -> None:
        await db.execute("DELETE FROM memory_chunks_fts")
        rows = await (await db.execute(
            "SELECT id, content, keywords FROM memory_chunks ORDER BY id"
        )).fetchall()
        for rowid, content, keywords in rows:
            await db.execute(
                "INSERT INTO memory_chunks_fts(rowid, content, keywords) VALUES (?, ?, ?)",
                (rowid, _tokenize_for_fts(str(content or "")), _tokenize_for_fts(str(keywords or ""))),
            )

    async def _delete_chunks(
        self, db: aiosqlite.Connection, agent_name: str, date: str | None = None
    ) -> None:
        """从三张表删除指定 agent（可选指定日期）的记忆块。"""
        if date is not None:
            where, params = "owner_agent = ? AND game_date = ?", (agent_name, date)
        else:
            where, params = "owner_agent = ?", (agent_name,)
        del_cursor = await db.execute(f"SELECT id FROM memory_chunks WHERE {where}", params)
        for (del_id,) in await del_cursor.fetchall():
            await db.execute("DELETE FROM memory_chunks_fts WHERE rowid = ?", (del_id,))
        await db.execute(
            f"DELETE FROM vec_memory_chunks WHERE rowid IN (SELECT id FROM memory_chunks WHERE {where})",
            params,
        )
        await db.execute(f"DELETE FROM memory_chunks WHERE {where}", params)

    async def _insert_chunks(
        self,
        db: aiosqlite.Connection,
        agent_name: str,
        payloads: list[tuple[str, str, str, str, int, str, str]],
    ) -> None:
        """embed + 写入三张表，不做删除。"""
        if not payloads:
            return
        embeddings = await embed_async([item[2] for item in payloads])
        for i, (memory_key, event_date, text, keywords, importance, c_hash, recalled_at) in enumerate(payloads):
            cur = await db.execute(
                "INSERT INTO memory_chunks(memory_key, owner_agent, game_date, content, keywords, importance, content_hash, last_recalled_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (memory_key, agent_name, event_date, text, keywords, importance, c_hash, recalled_at),
            )
            rowid = int(cur.lastrowid or 0)
            if rowid:
                await db.execute(
                    "INSERT OR REPLACE INTO vec_memory_chunks(rowid, embedding) VALUES (?, ?)",
                    (rowid, self._to_vec_blob(embeddings[i])),
                )
                await db.execute(
                    "INSERT INTO memory_chunks_fts(rowid, content, keywords) VALUES (?, ?, ?)",
                    (rowid, _tokenize_for_fts(text), _tokenize_for_fts(keywords)),
                )

    async def add(
        self,
        agent_name: str,
        date: str,
        chunks: list[tuple[str, str, int]],
    ) -> None:
        """将预处理的事件块 upsert 到向量库（先删同 agent+date 旧数据，再写入）。

        Args:
            agent_name: 角色名
            date: 规范化游戏日期（X月X日），由调用方保证格式正确
            chunks: [(text, keywords, importance), ...] 预提取的元数据
        """
        if not _CN_DATE_RE.match(date):
            routing_logger.warning(
                "[VectorStore] 跳过长期记忆索引: agent=%s, date=%s, 日期格式无效",
                agent_name, date,
            )
            return

        items = [(t.strip(), kw, imp) for t, kw, imp in chunks if t.strip()]
        if not items:
            routing_logger.info(
                "[VectorStore] 跳过长期记忆索引: agent=%s, date=%s, 无有效事件",
                agent_name, date,
            )
            return

        # sidecar fallback 可以在锁外读（仅文件 I/O，load 后才命中）
        sidecar_recall = self._sidecar_recall_by_hash(agent_name, date)

        db: aiosqlite.Connection | None = None
        try:
            async with self._get_write_lock():
                await self.init_tables()
                db = await self._get_db()

                # 在事务内查 recall 状态，避免与并发写竞争
                recall_by_hash = await self._query_db_recall_by_hash(agent_name, date)
                if not recall_by_hash:
                    recall_by_hash = sidecar_recall

                payloads: list[tuple[str, str, str, str, int, str, str]] = []
                for idx, (text, keywords, importance) in enumerate(items, start=1):
                    memory_key = f"memory::{agent_name}::{date}::{idx}"
                    content_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()
                    recalled_at = recall_by_hash.get(content_hash, date)
                    payloads.append((
                        memory_key, date, text, keywords, importance,
                        content_hash, recalled_at,
                    ))

                routing_logger.info(
                    "[VectorStore] 开始长期记忆索引: agent=%s, date=%s, 待写入事件=%s",
                    agent_name, date, len(payloads),
                )
                await db.execute("BEGIN")
                await self._delete_chunks(db, agent_name, date)
                await self._insert_chunks(db, agent_name, payloads)
                await db.commit()
                routing_logger.info(
                    "[VectorStore] 长期记忆索引完成: agent=%s, date=%s, 写入事件=%s",
                    agent_name, date, len(payloads)
                )
        except Exception as e:
            try:
                if db is not None:
                    await db.execute("ROLLBACK")
            except Exception:
                pass
            routing_logger.error(
                "[VectorStore] 索引长期记忆失败: agent=%s, date=%s, error=%s",
                agent_name, date, e
            )

    # ----------------------------- 原始检索 -----------------------------

    def get_vector_candidates(
        self,
        conn: sqlite3.Connection,
        agent_name: str,
        query_vec: list[float],
        limit: int,
    ) -> list[tuple]:
        """向量近邻检索，返回 (id, content, distance, game_date, last_recalled_at, importance) 列表。"""
        candidate_limit = max(limit * 10, 50)
        sql = """
        WITH scope AS (
          SELECT id FROM memory_chunks WHERE owner_agent = ?
        ),
        vec_results AS (
          SELECT rowid, distance FROM vec_memory_chunks
          WHERE embedding MATCH ?
          LIMIT ?
        )
        SELECT c.id, c.content, v.distance, c.game_date, c.last_recalled_at, c.importance
        FROM vec_results v
        JOIN scope s ON s.id = v.rowid
        JOIN memory_chunks c ON c.id = v.rowid
        ORDER BY v.distance
        LIMIT ?
        """
        return conn.execute(
            sql,
            (agent_name, self._to_vec_blob(query_vec), candidate_limit, limit),
        ).fetchall()

    @staticmethod
    def get_bm25_candidates(
        conn: sqlite3.Connection,
        agent_name: str,
        query_text: str,
        limit: int,
    ) -> list[tuple]:
        """BM25 全文检索，返回 (id, content, bm25_rank, game_date, last_recalled_at, importance) 列表。

        FTS5 的 rank 值为负数（越小越相关），这里保留原始值供调用方处理。
        """
        fts_query = _build_fts_match_query(query_text)
        if not fts_query:
            return []

        sql = """
        WITH scope AS (
          SELECT id FROM memory_chunks WHERE owner_agent = ?
        ),
        fts_hits AS (
          SELECT rowid, bm25(memory_chunks_fts, 1.0, 3.0) AS rank
          FROM memory_chunks_fts
          WHERE memory_chunks_fts MATCH ?
        )
        SELECT c.id, c.content, f.rank, c.game_date, c.last_recalled_at, c.importance
        FROM fts_hits f
        JOIN scope s ON s.id = f.rowid
        JOIN memory_chunks c ON c.id = f.rowid
        ORDER BY f.rank
        LIMIT ?
        """
        try:
            return conn.execute(sql, (agent_name, fts_query, limit)).fetchall()
        except Exception as e:
            routing_logger.warning("[VectorStore] BM25 检索失败（降级跳过）: %s", e)
            return []

    def update_recall_timestamps(
        self,
        conn: sqlite3.Connection,
        recalled_ids: list[str],
        current_game_date: str,
    ) -> None:
        """更新 DB 中 last_recalled_at。"""
        placeholders = ",".join("?" * len(recalled_ids))
        conn.execute(
            f"UPDATE memory_chunks SET last_recalled_at = ? WHERE id IN ({placeholders})",
            (current_game_date, *recalled_ids),
        )

    @staticmethod
    def _to_vec_blob(vec: list[float]) -> bytes:
        import array
        a = array.array("f", [float(x) for x in vec])
        return a.tobytes()

    # ----------------------------- 删除 -----------------------------

    async def delete(self, agent_name: str) -> bool:
        """删除指定 agent 的所有记忆索引。"""
        try:
            await self.init_tables()
            db = await self._get_db()
            await self._delete_chunks(db, agent_name)
            await db.commit()
            routing_logger.info("[VectorStore] 已清空 %s 的记忆索引", agent_name)
            return True
        except Exception as e:
            routing_logger.error("[VectorStore] 删除 %s 失败: %s", agent_name, e)
            return False

    async def delete_all_agents(self, agent_names: list[str]) -> dict[str, bool]:
        unique_names = list(dict.fromkeys(agent_names))
        if not unique_names:
            return {}

        all_agents = set(get_agent_names())
        if all_agents and set(unique_names) == all_agents:
            db: aiosqlite.Connection | None = None
            try:
                await self.init_tables()
                db = await self._get_db()
                async with self._get_write_lock():
                    await db.execute("BEGIN")
                    await db.execute("DELETE FROM vec_memory_chunks")
                    await db.execute("DELETE FROM memory_chunks_fts")
                    await db.execute("DELETE FROM memory_chunks")
                    await db.commit()
                routing_logger.info("[VectorStore] delete_all_agents 命中全角色，已全量清空记忆索引")
                return {name: True for name in unique_names}
            except Exception as e:
                try:
                    if db is not None:
                        await db.execute("ROLLBACK")
                except Exception:
                    pass
                routing_logger.error(f"[VectorStore] 全量清理失败，回退逐角色删除: {e}")

        results = await asyncio.gather(*(self.delete(name) for name in unique_names))
        return dict(zip(unique_names, results))


# 全局实例
vector_store = VectorStore()
