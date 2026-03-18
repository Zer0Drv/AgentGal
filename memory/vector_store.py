"""本地向量存储（sqlite-vec）。

仅持久化 memory 层的长期记忆事件，并按 relevance + 游戏时间 recency 检索。
"""

from __future__ import annotations

import math
import os
import json
import asyncio
import sqlite3
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import httpx

from log_config.memory import memory_logger as routing_logger
from engine.config import (
    BM25_CANDIDATE_LIMIT,
    HYBRID_SEARCH_ENABLED,
    RELEVANCE_WEIGHT,
    RECENCY_WEIGHT,
    RECENCY_HALF_LIFE_DAYS,
    RECENCY_DATE_WEIGHT,
    RECENCY_RECALL_WEIGHT,
    VECTOR_RELEVANCE_WEIGHT,
    BM25_RELEVANCE_WEIGHT,
    RERANK_CANDIDATE_MULTIPLIER,
    VECTOR_SEARCH_LIMIT,
    character_path,
    PROJECT_ROOT,
    get_agent_names,
)
from memory.file_ops import (
    extract_status_field,
    normalize,
    split_by_date,
    split_into_events,
    canonical_cn_date,
    game_day_diff,
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
RERANK_ENABLED = os.getenv("RERANK_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
RERANK_MODEL = os.getenv("RERANK_MODEL", "") if RERANK_ENABLED else ""
RERANK_API_KEY = os.getenv("RERANK_API_KEY") or EMBED_API_KEY
RERANK_API_URL = os.getenv("RERANK_API_URL", "")


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


def _vector_relevance_from_distance(distance: float) -> float:
    """将 sqlite-vec 的 distance 映射为 [0, 1] relevance。"""
    return max(0.0, 1.0 - min(float(distance), 2.0) / 2.0)


def _decay_from_game_date(current_game_date: str, past_game_date: str, half_life_days: float) -> float:
    """基于游戏内日期做指数衰减；无法解析时返回中性值。"""
    if half_life_days <= 0:
        return 0.5
    days_ago = game_day_diff(current_game_date, past_game_date)
    if days_ago is None:
        return 0.5
    return math.exp(-math.log(2) * days_ago / half_life_days)


class VectorStore:
    """sqlite-vec 本地向量库。

    表结构：
    - chunks(id INTEGER PK, round_id TEXT UNIQUE, date TEXT, visible_to TEXT, content TEXT)
    - vec_chunks USING vec0(embedding F32[EMBED_DIM])  -- rowid 对应 chunks.id
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
    def _content_hash(text: str) -> str:
        """生成事件文本的稳定哈希，用于重建时尽量找回旧 recall 状态。"""
        return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()

    @staticmethod
    def _memory_chunk_date(chunk_id: str) -> str | None:
        """从 memory chunk id 中提取所属日期。"""
        parts = chunk_id.split("::")
        if len(parts) != 4:
            return None
        return parts[2]

    def _resolve_memory_recall_date(
        self,
        state: dict[str, dict[str, Any]],
        chunk_id: str,
        event_date: str,
        content_hash: str,
    ) -> str:
        """为重建后的事件选择最合适的 last_recalled_at。"""
        exact = state.get(chunk_id, {})
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
        payloads: list[tuple[str, str, str, str]],
        previous_state: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """用当前日期的最新事件集合重写 recall sidecar。"""
        next_state = {
            key: value
            for key, value in previous_state.items()
            if self._memory_chunk_date(key) != date
        }
        for chunk_id, event_date, text, recalled_at in payloads:
            next_state[chunk_id] = {
                "date": event_date,
                "content_hash": self._content_hash(text),
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

    def _load_current_game_date(self) -> str | None:
        """从 narrator/status.md 读取当前游戏日。"""
        try:
            status = Path(self.character_path("narrator", "status.md")).read_text(
                encoding="utf-8"
            )
        except OSError:
            return None
        current_time = extract_status_field(status, "当前时间") if status else ""
        return canonical_cn_date(current_time)

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
        rows = await (
            await db.execute("SELECT id, date, last_recalled_at FROM chunks WHERE last_recalled_at IS NOT NULL")
        ).fetchall()
        for row_id, event_date, recalled_at in rows:
            if recalled_at and not parse_cn_date(str(recalled_at)):
                normalized_date = canonical_cn_date(str(event_date or ""))
                await db.execute(
                    "UPDATE chunks SET last_recalled_at = ? WHERE id = ?",
                    (normalized_date, row_id),
                )
        await db.commit()

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

        normalized_date = canonical_cn_date(date)
        if not normalized_date:
            routing_logger.warning(
                "[VectorStore] 跳过长期记忆索引: agent=%s, date=%s, 日期格式无效",
                agent_name, date,
            )
            return

        previous_state = self._read_memory_recall_state(agent_name)
        payloads: list[tuple[str, str, str, str]] = []
        events = split_into_events(body)
        for idx, event in enumerate(events, start=1):
            text = event.strip()
            if text:
                chunk_id = f"memory::{agent_name}::{normalized_date}::{idx}"
                content_hash = self._content_hash(text)
                recalled_at = self._resolve_memory_recall_date(
                    previous_state, chunk_id, normalized_date, content_hash
                )
                payloads.append((chunk_id, normalized_date, text, recalled_at))

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
                # 先查出要删除的 rowid，同步清理 FTS 和向量索引
                del_cursor = await db.execute(
                    "SELECT id FROM chunks WHERE source = 'memory' AND owner_agent = ? AND date = ?",
                    (agent_name, normalized_date),
                )
                del_rows = await del_cursor.fetchall()
                for (del_id,) in del_rows:
                    await db.execute("DELETE FROM chunks_fts WHERE rowid = ?", (del_id,))
                await db.execute(
                    "DELETE FROM vec_chunks WHERE rowid IN ("
                    "SELECT id FROM chunks WHERE source = 'memory' AND owner_agent = ? AND date = ?"
                    ")",
                    (agent_name, normalized_date),
                )
                await db.execute(
                    "DELETE FROM chunks WHERE source = 'memory' AND owner_agent = ? AND date = ?",
                    (agent_name, normalized_date),
                )

                if payloads:
                    embeddings = await _embed_async([item[2] for item in payloads])
                    visible_json = json.dumps([agent_name], ensure_ascii=False)
                    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    for i, (chunk_id, event_date, text, recalled_at) in enumerate(payloads):
                        cur = await db.execute(
                            "INSERT INTO chunks(round_id, date, created_at, visible_to, content, source, owner_agent, last_recalled_at) "
                            "VALUES (?, ?, ?, ?, ?, 'memory', ?, ?)",
                            (chunk_id, event_date, now_iso, visible_json, text, agent_name, recalled_at),
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
            await db.execute("DELETE FROM vec_chunks")
            await db.execute("DELETE FROM chunks_fts")
            await db.execute("DELETE FROM chunks")
            await db.commit()

            # ===== memory 层重建：根据 consolidation_state 之前的日期 =====
            for agent in get_agent_names():
                cutoff = self._read_consolidation_state(agent).get("last_consolidated_date")
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
        finally:
            if self._db is not None:
                await self._db.close()
                self._db = None

    # ----------------------------- 检索 -----------------------------

    @staticmethod
    def _build_scope_sql(agent_name: str, kind_norm: str) -> tuple[str, tuple]:
        """构建 memory-only 可见性过滤 SQL 片段。"""
        _ = kind_norm
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
        """纯向量近邻检索，返回 (id, content, distance, date, last_recalled_at) 列表。"""
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
        SELECT c.id, c.content, v.distance, c.date, c.last_recalled_at
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
    ) -> list[tuple[int, str, float, str, str]]:
        """BM25 全文检索，返回 (id, content, bm25_rank, date, last_recalled_at) 列表。

        FTS5 的 rank 值为负数（越小越相关），这里取绝对值转为正数分数。
        """
        fts_query = _build_fts_match_query(query)
        if not fts_query:
            return []

        sql = f"""
        WITH scope AS (
          {scope_sql}
        ),
        fts_hits AS (
          SELECT rowid, rank FROM chunks_fts WHERE chunks_fts MATCH ?
        )
        SELECT c.id, c.content, f.rank, c.date, c.last_recalled_at
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
    def _hybrid_relevance_fusion(
        vec_rows: list[tuple],
        bm25_rows: list[tuple],
    ) -> list[dict[str, Any]]:
        """按 75% vector + 25% BM25 组合 relevance。"""
        docs: dict[int, dict[str, Any]] = {}

        for row in vec_rows:
            doc_id = int(row[0])
            docs[doc_id] = {
                "id": str(doc_id),
                "content": row[1],
                "vector_relevance": _vector_relevance_from_distance(float(row[2])),
                "bm25_raw": None,
                "date": str(row[3] or ""),
                "last_recalled_at": str(row[4] or ""),
            }

        for row in bm25_rows:
            doc_id = int(row[0])
            entry = docs.setdefault(
                doc_id,
                {
                    "id": str(doc_id),
                    "content": row[1],
                    "vector_relevance": 0.0,
                    "bm25_raw": None,
                    "date": str(row[3] or ""),
                    "last_recalled_at": str(row[4] or ""),
                },
            )
            entry["content"] = row[1]
            entry["bm25_raw"] = abs(float(row[2]))
            if not entry["date"]:
                entry["date"] = str(row[3] or "")
            if not entry["last_recalled_at"]:
                entry["last_recalled_at"] = str(row[4] or "")

        bm25_values = [float(entry["bm25_raw"]) for entry in docs.values() if entry["bm25_raw"] is not None]
        bm25_min = min(bm25_values) if bm25_values else 0.0
        bm25_max = max(bm25_values) if bm25_values else 0.0

        results: list[dict[str, Any]] = []
        for entry in docs.values():
            bm25_raw = entry["bm25_raw"]
            if bm25_raw is None:
                bm25_relevance = 0.0
            elif bm25_max > bm25_min:
                bm25_relevance = (float(bm25_raw) - bm25_min) / (bm25_max - bm25_min)
            else:
                bm25_relevance = 1.0

            relevance = (
                VECTOR_RELEVANCE_WEIGHT * float(entry["vector_relevance"])
                + BM25_RELEVANCE_WEIGHT * bm25_relevance
            )
            results.append(
                {
                    "id": entry["id"],
                    "content": entry["content"],
                    "relevance": relevance,
                    "date": entry["date"],
                    "last_recalled_at": entry["last_recalled_at"],
                }
            )

        return sorted(results, key=lambda item: item["relevance"], reverse=True)

    def search(
        self,
        agent_name: str,
        query: str,
        limit: int | None = None,
        kind: str = "memory",
    ) -> list[dict[str, Any]]:
        """语义搜索：memory-only，按 relevance + 游戏时间 recency 排序。"""
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
            if kind_norm != "memory":
                routing_logger.info("[VectorStore] 非 memory 检索已停用: agent=%s, kind=%s", agent_name, kind_norm)
                return []
            scope_sql, scope_params = self._build_scope_sql(agent_name, kind_norm)
            current_game_date = self._load_current_game_date()

            # 若启用 rerank，先多取候选，rerank 后再截取
            rerank_multiplier = RERANK_CANDIDATE_MULTIPLIER
            fetch_limit = limit * rerank_multiplier if RERANK_MODEL else limit

            # 向量检索
            vec_rows = self._vector_search(conn, qvec, scope_sql, scope_params, fetch_limit)

            # 混合检索：75% vector relevance + 25% BM25 relevance
            if HYBRID_SEARCH_ENABLED:
                bm25_rows = self._bm25_search(
                    conn, query, scope_sql, scope_params, BM25_CANDIDATE_LIMIT,
                )
                if bm25_rows:
                    merged = self._hybrid_relevance_fusion(vec_rows, bm25_rows)
                    results = self._apply_recency_ranking(merged, current_game_date)[:fetch_limit]
                    routing_logger.info(
                        "[VectorStore] hybrid relevance + recency: agent=%s, vec=%s, bm25=%s, merged=%s",
                        agent_name, len(vec_rows), len(bm25_rows), len(results),
                    )
                else:
                    results = self._apply_vector_recency_ranking(vec_rows, current_game_date)
            else:
                results = self._apply_vector_recency_ranking(vec_rows, current_game_date)

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

            recalled_ids = [r["id"] for r in results if r["id"]]
            if recalled_ids and current_game_date:
                try:
                    placeholders = ",".join("?" * len(recalled_ids))
                    conn.execute(
                        f"UPDATE chunks SET last_recalled_at = ? WHERE id IN ({placeholders})",
                        (current_game_date, *recalled_ids),
                    )
                    conn.commit()
                    self._sync_recall_state_from_search(conn, recalled_ids, current_game_date)
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
    def _recency_score(
        current_game_date: str | None,
        event_date: str,
        last_recalled_at: str,
    ) -> float:
        """计算单条记忆的 recency，按配置权重融合日期与 recall 两个信号。"""
        if not current_game_date:
            return 0.5
        event_score = _decay_from_game_date(current_game_date, event_date, RECENCY_HALF_LIFE_DAYS)
        recall_score = _decay_from_game_date(current_game_date, last_recalled_at or event_date, RECENCY_HALF_LIFE_DAYS)
        weighted_signals: list[tuple[float, float]] = []
        if RECENCY_DATE_WEIGHT > 0:
            weighted_signals.append((event_score, RECENCY_DATE_WEIGHT))
        if RECENCY_RECALL_WEIGHT > 0 and last_recalled_at:
            weighted_signals.append((recall_score, RECENCY_RECALL_WEIGHT))
        if not weighted_signals:
            return 0.5
        total_weight = sum(weight for _, weight in weighted_signals)
        if total_weight <= 0:
            return 0.5
        return sum(score * weight for score, weight in weighted_signals) / total_weight

    @classmethod
    def _apply_vector_recency_ranking(
        cls,
        rows: list[tuple],
        current_game_date: str | None,
    ) -> list[dict[str, Any]]:
        """对纯向量候选补齐 relevance/recency 并完成最终排序。"""
        results = [
            {
                "id": str(row[0]),
                "content": row[1],
                "relevance": _vector_relevance_from_distance(float(row[2])),
                "date": str(row[3] or ""),
                "last_recalled_at": str(row[4] or ""),
            }
            for row in rows
        ]
        return cls._apply_recency_ranking(results, current_game_date)

    @classmethod
    def _apply_recency_ranking(
        cls,
        results: list[dict[str, Any]],
        current_game_date: str | None,
    ) -> list[dict[str, Any]]:
        """按配置权重融合 relevance 与 recency，输出最终返回结果。"""
        total_weight = RELEVANCE_WEIGHT + RECENCY_WEIGHT
        relevance_weight = RELEVANCE_WEIGHT / total_weight if total_weight > 0 else 0.7
        recency_weight = RECENCY_WEIGHT / total_weight if total_weight > 0 else 0.3

        ranked: list[dict[str, Any]] = []
        for result in results:
            recency = cls._recency_score(
                current_game_date,
                str(result.get("date", "")),
                str(result.get("last_recalled_at", "")),
            )
            relevance = float(result.get("relevance", 0.0))
            score = relevance_weight * relevance + recency_weight * recency
            ranked.append(
                {
                    "id": str(result["id"]),
                    "content": str(result["content"]),
                    "score": score,
                    "relevance": relevance,
                    "recency": recency,
                    "date": str(result.get("date", "")),
                    "last_recalled_at": str(result.get("last_recalled_at", "")),
                }
            )
        return sorted(ranked, key=lambda item: item["score"], reverse=True)

    def _sync_recall_state_from_search(
        self,
        conn: sqlite3.Connection,
        recalled_ids: list[str],
        current_game_date: str,
    ) -> None:
        """将命中结果的 recall 状态同步回 sidecar，供 rebuild()/load 恢复。"""
        placeholders = ",".join("?" * len(recalled_ids))
        rows = conn.execute(
            f"SELECT round_id, owner_agent, date, content FROM chunks WHERE id IN ({placeholders})",
            recalled_ids,
        ).fetchall()
        state_cache: dict[str, dict[str, dict[str, Any]]] = {}
        for chunk_id, owner_agent, event_date, content in rows:
            if not owner_agent:
                continue
            if owner_agent not in state_cache:
                state_cache[owner_agent] = self._read_memory_recall_state(owner_agent)
            state = state_cache[owner_agent]
            state[str(chunk_id)] = {
                "date": canonical_cn_date(str(event_date or "")) or "",
                "content_hash": self._content_hash(str(content or "")),
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
