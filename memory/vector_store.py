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
import re
from pathlib import Path
from typing import Any

import aiosqlite
import httpx

from log_config.memory import memory_logger as routing_logger
from engine.config import (
    character_path,
    PROJECT_ROOT,
    get_agent_names,
)
from memory.file_ops import (
    normalize,
    split_by_date,
    split_into_events,
    canonical_cn_date,
    parse_cn_date,
    is_date_before,
)


# ----------------------------- 配置与常量 -----------------------------

DB_PATH = str(PROJECT_ROOT / "data" / "vectors.sqlite")

# 默认使用 OpenAI 兼容 Embeddings 接口；兼容 EMBEDDING_MODEL 与 EMBEDDING_MODEL_ID 两种变量名
EMBED_MODEL = os.getenv("EMBEDDING_MODEL_ID") or os.getenv("EMBEDDING_MODEL") or "text-embedding-3-small"
EMBED_API_KEY = os.getenv("EMBEDDING_API_KEY") or os.getenv("LLM_API_KEY", "")
EMBED_API_URL = os.getenv("EMBEDDING_API_URL") or os.getenv("LLM_API_URL", "")

# 维度：根据模型选择，text-embedding-3-small=1536；兼容 .env 的 EMBEDDING_DIM
EMBED_DIM = int(os.getenv("EMBEDDING_DIM", "1536"))


# CJK Unicode 范围（用于 FTS5 预分词）
_CJK_RANGES = (
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0x3400, 0x4DBF),    # CJK Unified Ideographs Extension A
    (0xF900, 0xFAFF),    # CJK Compatibility Ideographs
)


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def _tokenize_for_fts(text: str) -> str:
    """在 CJK 字符间插入空格，使 unicode61 tokenizer 逐字拆分。

    非 CJK 文本（英文、数字）保持原样，按空格/标点自然分词。
    """
    result: list[str] = []
    for ch in text:
        if _is_cjk(ch):
            result.append(f" {ch} ")
        else:
            result.append(ch)
    # 压缩连续空格
    return " ".join("".join(result).split())


_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")


def _build_fts_match_query(text: str, max_terms: int = 24) -> str:
    """将自由文本转换为稳定的 FTS5 MATCH 查询。

    - 去掉 Markdown/FTS 特殊语法，避免 `*`、`:` 等触发解析错误
    - 中文按单字、英文按连续字母数字分词
    - 使用 OR 扩大召回面，由 BM25 自行排序
    """
    tokenized = _tokenize_for_fts(text.strip())
    if not tokenized:
        return ""

    terms: list[str] = []
    seen: set[str] = set()
    for raw_token in _FTS_TOKEN_RE.findall(tokenized):
        token = raw_token.lower()
        if token in seen:
            continue
        seen.add(token)
        terms.append(token)
        if len(terms) >= max_terms:
            break

    if not terms:
        return ""

    return " OR ".join(f'"{term}"' for term in terms)


# ----------------------------- 嵌入函数 -----------------------------

def _validate_embed_config():
    if not EMBED_API_KEY:
        raise ValueError("EMBEDDING_API_KEY 或 LLM_API_KEY 未配置，无法计算向量")
    if not EMBED_API_URL:
        raise ValueError("EMBEDDING_API_URL 或 LLM_API_URL 未配置，无法计算向量")


async def _embed_async(texts: list[str]) -> list[list[float]]:
    """异步计算嵌入"""
    _validate_embed_config()

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            EMBED_API_URL,
            headers={"Authorization": f"Bearer {EMBED_API_KEY}", "Content-Type": "application/json"},
            json={"model": EMBED_MODEL, "input": texts},
        )
        resp.raise_for_status()
        return [d["embedding"] for d in resp.json()["data"]]


def _embed_sync(texts: list[str]) -> list[list[float]]:
    """同步计算嵌入（用于同步检索路径）。"""
    _validate_embed_config()
    resp = httpx.post(
        EMBED_API_URL,
        headers={"Authorization": f"Bearer {EMBED_API_KEY}", "Content-Type": "application/json"},
        json={"model": EMBED_MODEL, "input": texts},
        timeout=60,
    )
    resp.raise_for_status()
    return [d["embedding"] for d in resp.json()["data"]]


class VectorStore:
    """sqlite-vec 本地向量库（memory-only）。

    表结构：
    - memory_chunks(id INTEGER PK, memory_key TEXT UNIQUE, owner_agent TEXT, game_date TEXT,
                     content TEXT, content_hash TEXT, last_recalled_at TEXT)
    - vec_memory_chunks USING vec0(embedding F32[EMBED_DIM])  -- rowid 对应 memory_chunks.id
    - memory_chunks_fts USING fts5(content)
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

    @staticmethod
    def _memory_key_date(memory_key: str) -> str | None:
        """从 memory_key 中提取所属日期。"""
        parts = memory_key.split("::")
        if len(parts) != 4:
            return None
        return parts[2]

    def _resolve_memory_recall_date(
        self,
        state: dict[str, dict[str, Any]],
        memory_key: str,
        event_date: str,
        content_hash: str,
    ) -> str:
        """为重建后的事件选择最合适的 last_recalled_at。"""
        exact = state.get(memory_key, {})
        recall = canonical_cn_date(str(exact.get("last_recalled_at", "")))
        if recall:
            return recall

        for entry in state.values():
            if not isinstance(entry, dict):
                continue
            if canonical_cn_date(str(entry.get("date", ""))) != event_date:
                continue
            if str(entry.get("content_hash", "")) == content_hash:
                recall = canonical_cn_date(str(entry.get("last_recalled_at", "")))
                if recall:
                    return recall

        return event_date

    def _read_json_object(self, path: Path) -> dict[str, Any]:
        """读取 sidecar JSON；解析失败返回空对象。"""
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _read_consolidation_state(self, agent_name: str) -> dict[str, Any]:
        """读取 consolidation sidecar。"""
        return self._read_json_object(
            Path(self.character_path(agent_name, ".consolidation_state.json"))
        )

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

    def _write_memory_recall_state(
        self,
        agent_name: str,
        state: dict[str, dict[str, Any]],
    ) -> None:
        """写回 recall sidecar。"""
        path = Path(self.character_path(agent_name, ".memory_recall_state.json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _rebuild_memory_recall_state(
        self,
        agent_name: str,
        date: str,
        payloads: list[tuple[str, str, str, str, str]],
        previous_state: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """用当前日期的最新事件集合重写 recall sidecar。

        payloads: [(memory_key, game_date, content, content_hash, recalled_at), ...]
        """
        next_state = {
            key: value
            for key, value in previous_state.items()
            if self._memory_key_date(key) != date
        }
        for memory_key, event_date, _text, content_hash, recalled_at in payloads:
            next_state[memory_key] = {
                "date": event_date,
                "content_hash": content_hash,
                "last_recalled_at": recalled_at,
            }
        try:
            self._write_memory_recall_state(agent_name, next_state)
        except Exception as e:
            routing_logger.warning(
                "[VectorStore] 写入 recall sidecar 失败: agent=%s, date=%s, error=%s",
                agent_name, date, e,
            )
        return next_state

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
                content_hash TEXT NOT NULL,
                last_recalled_at TEXT
            )
            """
        )
        await db.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_memory_chunks USING vec0(
                embedding F32[{EMBED_DIM}]
            )
            """
        )
        await db.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_fts USING fts5(
                content,
                tokenize='unicode61'
            )
            """
        )
        await db.commit()

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
        payloads: list[tuple[str, str, str, str, str]],
    ) -> None:
        """embed + 写入三张表，不做删除。payloads: (memory_key, game_date, text, content_hash, recalled_at)"""
        if not payloads:
            return
        embeddings = await _embed_async([item[2] for item in payloads])
        for i, (memory_key, event_date, text, c_hash, recalled_at) in enumerate(payloads):
            cur = await db.execute(
                "INSERT INTO memory_chunks(memory_key, owner_agent, game_date, content, content_hash, last_recalled_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (memory_key, agent_name, event_date, text, c_hash, recalled_at),
            )
            rowid = int(cur.lastrowid or 0)
            if rowid:
                await db.execute(
                    "INSERT OR REPLACE INTO vec_memory_chunks(rowid, embedding) VALUES (?, ?)",
                    (rowid, self._to_vec_blob(embeddings[i])),
                )
                await db.execute(
                    "INSERT INTO memory_chunks_fts(rowid, content) VALUES (?, ?)",
                    (rowid, _tokenize_for_fts(text)),
                )

    async def add(self, agent_name: str, date: str, chunks: list[str]) -> None:
        """将预分割的事件块 upsert 到向量库（先删同 agent+date 旧数据，再写入）。"""
        normalized_date = canonical_cn_date(date)
        if not normalized_date:
            routing_logger.warning(
                "[VectorStore] 跳过长期记忆索引: agent=%s, date=%s, 日期格式无效",
                agent_name, date,
            )
            return

        texts = [t.strip() for t in chunks if t.strip()]
        if not texts:
            routing_logger.info(
                "[VectorStore] 跳过长期记忆索引: agent=%s, date=%s, 无有效事件",
                agent_name, date,
            )
            return

        previous_state = self._read_memory_recall_state(agent_name)
        payloads: list[tuple[str, str, str, str, str]] = []
        for idx, text in enumerate(texts, start=1):
            memory_key = f"memory::{agent_name}::{normalized_date}::{idx}"
            content_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()
            recalled_at = self._resolve_memory_recall_date(
                previous_state, memory_key, normalized_date, content_hash
            )
            payloads.append((memory_key, normalized_date, text, content_hash, recalled_at))

        routing_logger.info(
            "[VectorStore] 开始长期记忆索引: agent=%s, date=%s, 待写入事件=%s",
            agent_name, normalized_date, len(payloads)
        )

        db: aiosqlite.Connection | None = None
        try:
            async with self._get_write_lock():
                await self.init_tables()
                db = await self._get_db()
                await db.execute("BEGIN")
                await self._delete_chunks(db, agent_name, normalized_date)
                await self._insert_chunks(db, agent_name, payloads)
                await db.commit()
                self._rebuild_memory_recall_state(
                    agent_name, normalized_date, payloads, previous_state
                )
                routing_logger.info(
                    "[VectorStore] 长期记忆索引完成: agent=%s, date=%s, 写入事件=%s",
                    agent_name, normalized_date, len(payloads)
                )
        except Exception as e:
            try:
                if db is not None:
                    await db.execute("ROLLBACK")
            except Exception:
                pass
            routing_logger.error(
                "[VectorStore] 索引长期记忆失败: agent=%s, date=%s, error=%s",
                agent_name, normalized_date, e
            )

    # ----------------------------- 重建 -----------------------------

    async def rebuild(self, agent_name: str):
        """重建：从各 agent 的 memory.md 重建 memory 层向量索引。"""
        _ = agent_name  # 保持外部调用签名兼容
        try:
            await self.init_tables()
            db = await self._get_db()

            # 清空
            await db.execute("DELETE FROM vec_memory_chunks")
            await db.execute("DELETE FROM memory_chunks_fts")
            await db.execute("DELETE FROM memory_chunks")
            await db.commit()

            # ===== memory 层重建：根据 consolidation_state 之前的日期 =====
            for agent in get_agent_names(include_narrator=False):
                cutoff = self._read_consolidation_state(agent).get("last_consolidated_date")
                if not cutoff or not parse_cn_date(cutoff):
                    routing_logger.info(
                        "[VectorStore] 重建跳过 memory: agent=%s, cutoff无效=%s",
                        agent, cutoff
                    )
                    continue

                path = Path(self.character_path(agent, "memory.md"))
                if not path.exists():
                    routing_logger.info(
                        "[VectorStore] 重建跳过 memory: agent=%s, 未找到memory文件", agent
                    )
                    continue

                sections = split_by_date(normalize(path.read_text(encoding="utf-8")))
                dates = [d for d in sections.keys() if is_date_before(d, cutoff)]
                for date in dates:
                    await self.add(agent, date, split_into_events(sections[date]))
        finally:
            if self._db is not None:
                await self._db.close()
                self._db = None

    # ----------------------------- 原始检索 -----------------------------

    def get_vector_candidates(
        self,
        conn: sqlite3.Connection,
        agent_name: str,
        query_vec: list[float],
        limit: int,
    ) -> list[tuple]:
        """向量近邻检索，返回 (id, content, distance, game_date, last_recalled_at) 列表。"""
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
        SELECT c.id, c.content, v.distance, c.game_date, c.last_recalled_at
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
        """BM25 全文检索，返回 (id, content, bm25_rank, game_date, last_recalled_at) 列表。

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
          SELECT rowid, rank FROM memory_chunks_fts WHERE memory_chunks_fts MATCH ?
        )
        SELECT c.id, c.content, f.rank, c.game_date, c.last_recalled_at
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
        """更新 DB 中 last_recalled_at，并同步写回 sidecar。"""
        placeholders = ",".join("?" * len(recalled_ids))
        conn.execute(
            f"UPDATE memory_chunks SET last_recalled_at = ? WHERE id IN ({placeholders})",
            (current_game_date, *recalled_ids),
        )
        # 同步 sidecar
        rows = conn.execute(
            f"SELECT memory_key, owner_agent, game_date, content FROM memory_chunks WHERE id IN ({placeholders})",
            recalled_ids,
        ).fetchall()
        state_cache: dict[str, dict[str, dict[str, Any]]] = {}
        for memory_key, owner_agent, event_date, content in rows:
            if not owner_agent:
                continue
            if owner_agent not in state_cache:
                state_cache[owner_agent] = self._read_memory_recall_state(owner_agent)
            state = state_cache[owner_agent]
            state[str(memory_key)] = {
                "date": canonical_cn_date(str(event_date or "")) or "",
                "content_hash": hashlib.sha1(str(content or "").strip().encode("utf-8")).hexdigest(),
                "last_recalled_at": current_game_date,
            }
        for agent_name, state in state_cache.items():
            try:
                self._write_memory_recall_state(agent_name, state)
            except Exception as e:
                routing_logger.warning(
                    "[VectorStore] 写回 recall sidecar 失败: agent=%s, error=%s",
                    agent_name, e,
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
