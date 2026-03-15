"""本地向量存储（sqlite-vec）

- 每轮对话后立即索引（每轮 = 从一条玩家消息到下一条玩家消息之间）。
- 每轮作为一个 chunk 入库；为该轮所有可见角色各写一条（方便按可见性过滤）。
- 检索时仅在当前角色可见的 chunks 中做 ANN 检索。
"""

from __future__ import annotations

import math
import os
import json
import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import httpx

from log_config.memory import memory_logger as routing_logger
from engine.config import (
    BM25_CANDIDATE_LIMIT,
    HYBRID_SEARCH_ENABLED,
    RERANK_CANDIDATE_MULTIPLIER,
    RRF_K,
    TIME_DECAY_ALPHA,
    TIME_DECAY_HALF_LIFE_DAYS,
    VECTOR_SEARCH_LIMIT,
    character_path,
    PROJECT_ROOT,
    get_agent_names,
)
from memory.file_ops import (
    load_consolidation_state,
    normalize,
    split_by_date,
    split_into_events,
    extract_game_date,
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

# Rerank 配置（可选；未配置则跳过 rerank 步骤）
RERANK_MODEL = os.getenv("RERANK_MODEL", "")
RERANK_API_KEY = os.getenv("RERANK_API_KEY") or EMBED_API_KEY
RERANK_API_URL = os.getenv("RERANK_API_URL", "")

# Time-decay / 混合检索配置从 config.toml 加载（见 engine/config.py）

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


def _rerank_sync(query: str, documents: list[str], top_n: int) -> list[int]:
    """调用 rerank API，返回按相关性降序排列的原始索引列表。

    兼容 OpenAI 兼容 rerank 端点（Jina / Cohere / Voyage 等）。
    响应格式：{"results": [{"index": int, "relevance_score": float}, ...]}
    """
    resp = httpx.post(
        RERANK_API_URL,
        headers={"Authorization": f"Bearer {RERANK_API_KEY}", "Content-Type": "application/json"},
        json={"model": RERANK_MODEL, "query": query, "documents": documents, "top_n": top_n},
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json()["results"]
    return [r["index"] for r in sorted(results, key=lambda x: x["relevance_score"], reverse=True)]


def _time_decay_score(created_at: str, half_life_days: float) -> float:
    """指数衰减时间得分：score = 0.5^(days_ago / half_life_days)，越新越接近 1.0。"""
    try:
        ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        days_ago = (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
        return math.exp(-math.log(2) * days_ago / half_life_days)
    except Exception:
        return 0.5  # 无法解析时返回中性值


class VectorStore:
    """sqlite-vec 本地向量库。

    表结构：
    - chunks(id INTEGER PK, round_id TEXT UNIQUE, date TEXT, visible_to TEXT, content TEXT)
    - vec_chunks USING vec0(embedding F32[EMBED_DIM])  -- rowid 对应 chunks.id
    """

    def __init__(self):
        self._db: aiosqlite.Connection | None = None
        self._conv_game_date: dict[str, str] = {}
        self.character_path = character_path
        self._background_tasks: set[asyncio.Task] = set()
        # 单连接下显式串行化写事务；按事件循环懒初始化避免跨 loop 复用报错
        self._write_lock: asyncio.Lock | None = None
        self._write_lock_loop: asyncio.AbstractEventLoop | None = None
        self._memory_index_cutoff: dict[str, str] = {}

    def _get_write_lock(self) -> asyncio.Lock:
        """获取当前事件循环绑定的写锁。"""
        loop = asyncio.get_running_loop()
        if self._write_lock is None or self._write_lock_loop is not loop:
            self._write_lock = asyncio.Lock()
            self._write_lock_loop = loop
        return self._write_lock

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

    async def init_tables(self):
        db = await self._get_db()
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id TEXT NOT NULL UNIQUE,
                date TEXT,
                created_at TEXT,
                visible_to TEXT,
                content TEXT NOT NULL
            )
            """
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_round_id ON chunks(round_id)"
        )
        cols = {
            row[1]
            for row in await (await db.execute("PRAGMA table_info(chunks)")).fetchall()
        }
        if "source" not in cols:
            await db.execute("ALTER TABLE chunks ADD COLUMN source TEXT NOT NULL DEFAULT 'round'")
        if "owner_agent" not in cols:
            await db.execute("ALTER TABLE chunks ADD COLUMN owner_agent TEXT")
        if "last_recalled_at" not in cols:
            await db.execute("ALTER TABLE chunks ADD COLUMN last_recalled_at TEXT")
        await db.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
                embedding F32[{EMBED_DIM}]
            )
            """
        )
        # FTS5 全文索引（独立存储，手动与 chunks 表同步）
        await db.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                content,
                tokenize='unicode61'
            )
            """
        )
        await db.commit()

    # ----------------------------- 写入 -----------------------------


    async def _do_add(
        self,
        visible_to: list[str],
        chunk_id: str,
        content: str,
        game_date: str | None,
        kind: str,
        owner_agent: str | None,
    ):
        """实际执行入库（内部方法）。"""
        # 解析会话 ID（仅 round 使用）
        conv_id = chunk_id.rsplit("_", 1)[0]

        # 统一可见性：调用方已保证含 narrator
        visible = list(dict.fromkeys(visible_to))

        # 解析或沿用"游戏日期"
        if game_date is None:
            game_date = extract_game_date(content)
        if game_date and kind in ("round", "dialogue"):
            self._conv_game_date[conv_id] = game_date
        if kind in ("round", "dialogue"):
            game_date = game_date or self._conv_game_date.get(conv_id, "")

        source = "memory" if kind == "memory" else "round"

        # 计算 embedding
        try:
            embeddings = await _embed_async([content])
            embedding = embeddings[0]
        except Exception as e:
            routing_logger.error(f"[VectorStore] 计算嵌入失败: chunk_id={chunk_id}, error={e}")
            return

        # 入库
        db: aiosqlite.Connection | None = None
        try:
            async with self._get_write_lock():
                await self.init_tables()
                db = await self._get_db()
                now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                visible_json = json.dumps(visible, ensure_ascii=False)

                await db.execute("BEGIN")

                # 避免 INSERT OR REPLACE 触发 delete+insert 导致 rowid 变化，
                # 进而在 vec_chunks 残留旧向量行。这里改为显式 update/insert，保持 rowid 稳定。
                cur = await db.execute(
                    "SELECT id FROM chunks WHERE round_id = ?",
                    (chunk_id,),
                )
                existing = await cur.fetchone()
                if existing:
                    rowid = int(existing[0])
                    await db.execute(
                        """
                        UPDATE chunks
                        SET date = ?, created_at = ?, visible_to = ?, content = ?, source = ?, owner_agent = ?
                        WHERE id = ?
                        """,
                        (game_date, now_iso, visible_json, content, source, owner_agent, rowid),
                    )
                else:
                    cur = await db.execute(
                        """
                        INSERT INTO chunks(round_id, date, created_at, visible_to, content, source, owner_agent)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (chunk_id, game_date, now_iso, visible_json, content, source, owner_agent),
                    )
                    rowid = int(cur.lastrowid or 0)

                # 插入向量
                if rowid:
                    blob = self._to_vec_blob(embedding)
                    await db.execute(
                        "INSERT OR REPLACE INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
                        (rowid, blob),
                    )
                    # 同步 FTS5 索引
                    if existing:
                        await db.execute("DELETE FROM chunks_fts WHERE rowid = ?", (rowid,))
                    await db.execute(
                        "INSERT INTO chunks_fts(rowid, content) VALUES (?, ?)",
                        (rowid, _tokenize_for_fts(content)),
                    )

                await db.commit()
            routing_logger.info(f"[VectorStore] 入库完成: chunk_id={chunk_id}, source={source}")
        except Exception as e:
            try:
                if db is not None:
                    await db.execute("ROLLBACK")
            except Exception:
                pass
            routing_logger.error(f"[VectorStore] 写入失败: chunk_id={chunk_id}, error={e}")

    async def add_memory(self, agent_name: str, date: str):
        """将 memory.md 指定日期的事件块添加到向量库。"""
        path = Path(self.character_path(agent_name, "memory.md"))
        if not path.exists():
            alt = Path(self.character_path(agent_name, "Memory.md"))
            if not alt.exists():
                routing_logger.info(
                    "[VectorStore] 跳过长期记忆索引: agent=%s, 未找到memory文件",
                    agent_name
                )
                return
            path = alt

        raw = path.read_text(encoding="utf-8")
        sections = split_by_date(normalize(raw))
        body = sections.get(date)
        if not body:
            routing_logger.info(
                "[VectorStore] 跳过长期记忆索引: agent=%s, date=%s, 未找到日期内容",
                agent_name, date
            )
            return

        payloads: list[tuple[str, str, str]] = []
        events = split_into_events(body)
        for idx, event in enumerate(events, start=1):
            text = event.strip()
            if text:
                payloads.append((f"memory::{agent_name}::{date}::{idx}", date, text))

        routing_logger.info(
            "[VectorStore] 开始长期记忆索引: agent=%s, date=%s, 待写入事件=%s",
            agent_name, date, len(payloads)
        )

        db: aiosqlite.Connection | None = None
        try:
            async with self._get_write_lock():
                await self.init_tables()
                db = await self._get_db()

                await db.execute("BEGIN")
                # 先查出要删除的 rowid，同步清理 FTS 和向量索引
                del_cursor = await db.execute(
                    "SELECT id FROM chunks WHERE source = 'memory' AND owner_agent = ? AND date = ?",
                    (agent_name, date),
                )
                del_rows = await del_cursor.fetchall()
                for (del_id,) in del_rows:
                    await db.execute("DELETE FROM chunks_fts WHERE rowid = ?", (del_id,))
                await db.execute(
                    "DELETE FROM vec_chunks WHERE rowid IN ("
                    "SELECT id FROM chunks WHERE source = 'memory' AND owner_agent = ? AND date = ?"
                    ")",
                    (agent_name, date),
                )
                await db.execute(
                    "DELETE FROM chunks WHERE source = 'memory' AND owner_agent = ? AND date = ?",
                    (agent_name, date),
                )

                if payloads:
                    embeddings = await _embed_async([item[2] for item in payloads])
                    visible_json = json.dumps([agent_name], ensure_ascii=False)
                    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    for i, (chunk_id, date, text) in enumerate(payloads):
                        cur = await db.execute(
                            "SELECT id FROM chunks WHERE round_id = ?",
                            (chunk_id,),
                        )
                        row = await cur.fetchone()
                        if row:
                            rowid = int(row[0])
                            await db.execute(
                                "UPDATE chunks SET date = ?, created_at = ?, visible_to = ?, "
                                "content = ?, source = 'memory', owner_agent = ? WHERE id = ?",
                                (date, now_iso, visible_json, text, agent_name, rowid),
                            )
                            # FTS: 更新需先删后插
                            await db.execute("DELETE FROM chunks_fts WHERE rowid = ?", (rowid,))
                        else:
                            cur = await db.execute(
                                "INSERT INTO chunks(round_id, date, created_at, visible_to, content, source, owner_agent) "
                                "VALUES (?, ?, ?, ?, ?, 'memory', ?)",
                                (chunk_id, date, now_iso, visible_json, text, agent_name),
                            )
                            rowid = int(cur.lastrowid or 0)
                        if rowid:
                            await db.execute(
                                "INSERT OR REPLACE INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
                                (rowid, self._to_vec_blob(embeddings[i])),
                            )
                            await db.execute(
                                "INSERT INTO chunks_fts(rowid, content) VALUES (?, ?)",
                                (rowid, _tokenize_for_fts(text)),
                            )

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

    # ----------------------------- 重建 -----------------------------

    async def rebuild(self, agent_name: str):
        """重建：从各 agent 的 memory.md 重建 memory 层向量索引。"""
        _ = agent_name  # 保持外部调用签名兼容
        await self.init_tables()
        db = await self._get_db()

        # 清空
        await db.execute("DELETE FROM vec_chunks")
        await db.execute("DELETE FROM chunks_fts")
        await db.execute("DELETE FROM chunks")
        await db.commit()

        # ===== memory 层重建：根据 consolidation_state 之前的日期 =====
        for agent in get_agent_names():
            cutoff = load_consolidation_state(agent)
            if not cutoff or not parse_cn_date(cutoff):
                routing_logger.info(
                    "[VectorStore] 重建跳过 memory: agent=%s, cutoff无效=%s",
                    agent, cutoff
                )
                continue

            path = Path(self.character_path(agent, "memory.md"))
            if not path.exists():
                alt = Path(self.character_path(agent, "Memory.md"))
                if not alt.exists():
                    routing_logger.info(
                        "[VectorStore] 重建跳过 memory: agent=%s, 未找到memory文件",
                        agent
                    )
                    continue
                path = alt

            raw = path.read_text(encoding="utf-8")
            sections = split_by_date(normalize(raw))
            dates = [d for d in sections.keys() if is_date_before(d, cutoff)]
            if not dates:
                continue

            for date in dates:
                await self.add_memory(agent, date)

    # ----------------------------- 检索 -----------------------------

    @staticmethod
    def _build_scope_sql(agent_name: str, kind_norm: str) -> tuple[str, tuple]:
        """构建可见性过滤 SQL 片段（供向量检索和 BM25 共用）。"""
        if kind_norm in ("round", "dialogue"):
            return (
                "SELECT id FROM chunks WHERE source = 'round' AND EXISTS ("
                "SELECT 1 FROM json_each(chunks.visible_to) WHERE json_each.value = ?"
                ")",
                (agent_name,),
            )
        elif kind_norm == "all":
            return (
                "SELECT id FROM chunks WHERE ("
                "source = 'memory' AND owner_agent = ?"
                ") OR ("
                "source = 'round' AND EXISTS ("
                "SELECT 1 FROM json_each(chunks.visible_to) WHERE json_each.value = ?"
                ")"
                ")",
                (agent_name, agent_name),
            )
        else:
            return (
                "SELECT id FROM chunks WHERE source = 'memory' AND owner_agent = ?",
                (agent_name,),
            )

    def _vector_search(
        self,
        conn: sqlite3.Connection,
        qvec: list[float],
        scope_sql: str,
        scope_params: tuple,
        fetch_limit: int,
    ) -> list[tuple]:
        """纯向量近邻检索，返回 (id, content, distance, time_ref) 列表。"""
        candidate_limit = max(fetch_limit * 10, 50)
        sql = f"""
        WITH scope AS (
          {scope_sql}
        ),
        vec_results AS (
          SELECT rowid, distance FROM vec_chunks
          WHERE embedding MATCH ?
          LIMIT ?
        )
        SELECT c.id, c.content, v.distance,
               COALESCE(c.last_recalled_at, c.created_at) AS time_ref
        FROM vec_results v
        JOIN scope s ON s.id = v.rowid
        JOIN chunks c ON c.id = v.rowid
        ORDER BY v.distance
        LIMIT ?
        """
        return conn.execute(
            sql,
            (*scope_params, self._to_vec_blob(qvec), candidate_limit, fetch_limit),
        ).fetchall()

    @staticmethod
    def _bm25_search(
        conn: sqlite3.Connection,
        query: str,
        scope_sql: str,
        scope_params: tuple,
        limit: int,
    ) -> list[tuple[int, str, float]]:
        """BM25 全文检索，返回 (id, content, bm25_rank) 列表。

        FTS5 的 rank 值为负数（越小越相关），这里取绝对值转为正数分数。
        """
        fts_query = _tokenize_for_fts(query.strip())
        if not fts_query:
            return []

        sql = f"""
        WITH scope AS (
          {scope_sql}
        ),
        fts_hits AS (
          SELECT rowid, rank FROM chunks_fts WHERE chunks_fts MATCH ?
        )
        SELECT c.id, c.content, f.rank
        FROM fts_hits f
        JOIN scope s ON s.id = f.rowid
        JOIN chunks c ON c.id = f.rowid
        ORDER BY f.rank
        LIMIT ?
        """
        try:
            return conn.execute(sql, (*scope_params, fts_query, limit)).fetchall()
        except Exception as e:
            routing_logger.warning("[VectorStore] BM25 检索失败（降级跳过）: %s", e)
            return []

    @staticmethod
    def _rrf_fusion(
        vec_rows: list[tuple],
        bm25_rows: list[tuple[int, str, float]],
        k: int,
    ) -> list[dict[str, Any]]:
        """RRF（Reciprocal Rank Fusion）合并两路检索结果。

        score(d) = 1/(k + rank_vec(d)) + 1/(k + rank_bm25(d))
        未出现在某一路的文档，该路贡献为 0。
        """
        scores: dict[int, float] = {}
        contents: dict[int, str] = {}

        # 向量路：vec_rows = [(id, content, distance, time_ref), ...]
        for rank, row in enumerate(vec_rows):
            doc_id = int(row[0])
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
            contents[doc_id] = row[1]

        # BM25 路：bm25_rows = [(id, content, rank_score), ...]
        for rank, row in enumerate(bm25_rows):
            doc_id = int(row[0])
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
            contents[doc_id] = row[1]

        # 按 RRF 分数降序排列
        sorted_ids = sorted(scores.keys(), key=lambda d: scores[d], reverse=True)
        return [
            {"id": str(doc_id), "content": contents[doc_id], "score": scores[doc_id]}
            for doc_id in sorted_ids
        ]

    def search(
        self,
        agent_name: str,
        query: str,
        limit: int | None = None,
        kind: str = "memory",
    ) -> list[dict[str, Any]]:
        """语义搜索（可选混合 BM25 + RRF）：按 kind 在可见范围内检索。"""
        if not query or not query.strip():
            return []

        if not isinstance(limit, int) or limit <= 0:
            limit = VECTOR_SEARCH_LIMIT

        # 计算查询向量（同步）
        try:
            qvec = _embed_sync([query])[0]
        except Exception as e:
            routing_logger.error(f"[VectorStore] 查询嵌入失败: {e}")
            return []

        # 执行检索
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(DB_PATH)
            self._load_sqlite_vec_sync(conn)

            kind_norm = (kind or "memory").strip().lower()
            scope_sql, scope_params = self._build_scope_sql(agent_name, kind_norm)

            # 若启用 rerank，先多取候选，rerank 后再截取
            rerank_multiplier = RERANK_CANDIDATE_MULTIPLIER
            fetch_limit = limit * rerank_multiplier if RERANK_MODEL else limit

            # 向量检索
            vec_rows = self._vector_search(conn, qvec, scope_sql, scope_params, fetch_limit)

            # 混合检索：BM25 + RRF
            if HYBRID_SEARCH_ENABLED:
                bm25_rows = self._bm25_search(
                    conn, query, scope_sql, scope_params, BM25_CANDIDATE_LIMIT,
                )
                if bm25_rows:
                    results = self._rrf_fusion(vec_rows, bm25_rows, RRF_K)[:fetch_limit]
                    routing_logger.info(
                        "[VectorStore] RRF 融合: agent=%s, vec=%s, bm25=%s, merged=%s",
                        agent_name, len(vec_rows), len(bm25_rows), len(results),
                    )
                else:
                    # BM25 无结果，降级为纯向量
                    results = self._apply_time_decay(vec_rows)
            else:
                results = self._apply_time_decay(vec_rows)

            if RERANK_MODEL and results:
                try:
                    ranked_indices = _rerank_sync(query, [r["content"] for r in results], top_n=limit)
                    results = [results[i] for i in ranked_indices][:limit]
                    routing_logger.info(
                        "[VectorStore] rerank 完成: agent=%s, 候选=%s, 返回=%s",
                        agent_name, len(vec_rows), len(results),
                    )
                except Exception as e:
                    routing_logger.warning(
                        "[VectorStore] rerank 失败，降级为 ANN 结果: agent=%s, error=%s",
                        agent_name, e,
                    )
                    results = results[:limit]
            else:
                results = results[:limit]

            # 更新 last_recalled_at：被返回的记忆视为"被想起"，遗忘时钟重置
            recalled_ids = [r["id"] for r in results if r["id"]]
            if recalled_ids and TIME_DECAY_ALPHA < 1.0:
                try:
                    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    placeholders = ",".join("?" * len(recalled_ids))
                    conn.execute(
                        f"UPDATE chunks SET last_recalled_at = ? WHERE id IN ({placeholders})",
                        (now_iso, *recalled_ids),
                    )
                    conn.commit()
                except Exception as e:
                    routing_logger.warning("[VectorStore] 更新 last_recalled_at 失败: %s", e)

            routing_logger.info(
                "[VectorStore] 搜索完成: agent=%s, limit=%s, 命中=%s, kind=%s, hybrid=%s",
                agent_name, limit, len(results), kind_norm, HYBRID_SEARCH_ENABLED,
            )

            return results
        except Exception as e:
            routing_logger.error(f"[VectorStore] 检索失败: {e}")
            return []
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _apply_time_decay(rows: list[tuple]) -> list[dict[str, Any]]:
        """对向量检索结果应用 time-decay 融合并转为 dict 列表。"""
        if TIME_DECAY_ALPHA < 1.0 and TIME_DECAY_HALF_LIFE_DAYS > 0 and rows:
            def _blended(row: tuple) -> float:
                dist, time_ref = row[2], row[3]
                similarity = 1.0 - min(float(dist), 2.0) / 2.0
                time_score = _time_decay_score(time_ref or "", TIME_DECAY_HALF_LIFE_DAYS)
                return TIME_DECAY_ALPHA * similarity + (1.0 - TIME_DECAY_ALPHA) * time_score

            rows = sorted(rows, key=_blended, reverse=True)

        return [{"id": str(r[0]), "content": r[1], "score": float(r[2])} for r in rows]

    @staticmethod
    def _to_vec_blob(vec: list[float]) -> bytes:
        import array
        a = array.array("f", [float(x) for x in vec])
        return a.tobytes()

    # ----------------------------- 删除 -----------------------------

    async def delete(self, agent_name: str) -> bool:
        """删除指定 agent 的所有向量与文本 chunk。"""
        try:
            await self.init_tables()
            db = await self._get_db()
            # 查出待删除 rowid，同步清理 FTS
            del_cursor = await db.execute(
                "SELECT id FROM chunks WHERE EXISTS (\n"
                "  SELECT 1 FROM json_each(visible_to) WHERE json_each.value = ?\n"
                ")",
                (agent_name,),
            )
            del_rows = await del_cursor.fetchall()
            for (del_id,) in del_rows:
                await db.execute("DELETE FROM chunks_fts WHERE rowid = ?", (del_id,))
            await db.execute(
                "DELETE FROM vec_chunks WHERE rowid IN (\n"
                "  SELECT id FROM chunks WHERE EXISTS (\n"
                "    SELECT 1 FROM json_each(visible_to) WHERE json_each.value = ?\n"
                "  )\n"
                ")",
                (agent_name,),
            )
            await db.execute(
                "DELETE FROM chunks WHERE EXISTS (\n"
                "  SELECT 1 FROM json_each(visible_to) WHERE json_each.value = ?\n"
                ")",
                (agent_name,),
            )
            await db.commit()
            routing_logger.info(f"[VectorStore] 已清空 {agent_name} 的向量与 chunks")
            return True
        except Exception as e:
            routing_logger.error(f"[VectorStore] 删除 {agent_name} 失败: {e}")
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
                    await db.execute("DELETE FROM vec_chunks")
                    await db.execute("DELETE FROM chunks_fts")
                    await db.execute("DELETE FROM chunks")
                    await db.commit()
                self._conv_game_date.clear()
                self._memory_index_cutoff.clear()
                routing_logger.info("[VectorStore] delete_all_agents 命中全角色，已全量清空向量库")
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
