"""本地向量存储（sqlite-vec）

- 每轮对话后立即索引（每轮 = 从一条玩家消息到下一条玩家消息之间）。
- 每轮作为一个 chunk 入库；为该轮所有可见角色各写一条（方便按可见性过滤）。
- 检索时仅在当前角色可见的 chunks 中做 ANN 检索。
"""

from __future__ import annotations

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
from engine.config import character_path, PROJECT_ROOT, get_agent_names
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
        await db.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
                embedding F32[{EMBED_DIM}]
            )
            """
        )
        await db.commit()

    # ----------------------------- 写入 -----------------------------

    def add(
        self,
        visible_to: list[str],
        chunk_id: str,
        content: str,
        game_date: str | None = None,
        kind: str = "round",
        owner_agent: str | None = None,
    ):
        """写入一个 chunk，后台写入数据库（不阻塞）。

        - kind: "round"/"dialogue" 或 "memory"
        - visible_to 决定可见范围；memory 默认仅 owner_agent 可见
        - 失败时仅记录日志，不抛异常影响主流程。
        """
        task = asyncio.create_task(
            self._do_add(visible_to, chunk_id, content, game_date, kind, owner_agent)
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def add_round(self, visible_to: list[str], round_id: str, content: str, game_date: str | None = None):
        """兼容旧接口：对话轮次写入。"""
        self.add(visible_to, round_id, content, game_date, kind="round")

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
        """重建：解析 jsonl（round）+ memory.md（memory） → 直接入库。"""
        import glob

        _ = agent_name  # 保持外部调用签名兼容
        await self.init_tables()
        db = await self._get_db()

        # 清空
        await db.execute("DELETE FROM vec_chunks")
        await db.execute("DELETE FROM chunks")
        await db.commit()

        raw_dir = Path(self.character_path("narrator", "raw"))
        files = sorted(glob.glob(str(raw_dir / "*.jsonl")))
        if not files:
            routing_logger.info("[VectorStore] 重建: 未发现任何原始对话记录，跳过")
            return

        def _iter_rounds():
            for fp in files:
                with open(fp, "r", encoding="utf-8") as f:
                    cur: list[dict] = []
                    for line in f:
                        if not line.strip():
                            continue
                        obj = json.loads(line)
                        if obj.get("role") == "player" and cur:
                            yield cur
                            cur = [obj]
                        else:
                            cur.append(obj)
                    if cur:
                        yield cur

        def _format_round(msgs: list[dict]) -> tuple[str, list[str], str | None]:
            parts = []
            for m in msgs:
                role = m.get("role", "unknown")
                text = m.get("content", "")
                if role == "player":
                    parts.append(f"玩家: {text}")
                elif role == "narrator":
                    parts.append(f"旁白: {text}")
                else:
                    parts.append(f"{role}: {text}")
            content = "\n".join(parts)

            vis_set, vis = set(), []
            for m in msgs:
                v = m.get("visible_to", [])
                if isinstance(v, str):
                    try:
                        v = json.loads(v)
                    except Exception:
                        v = []
                for x in v or []:
                    x = str(x)
                    if x not in vis_set:
                        vis_set.add(x)
                        vis.append(x)
            if "narrator" not in vis_set:
                vis.append("narrator")

            gdate = None
            for m in msgs:
                if m.get("role") != "narrator":
                    continue
                gdate = extract_game_date(str(m.get("content", "")))
                if gdate:
                    break
            return content, vis, gdate

        counter = 0
        for msgs in _iter_rounds():
            counter += 1
            content, vis, gdate = _format_round(msgs)
            round_id = f"rebuild_{counter}"
            await self._do_add(vis, round_id, content, gdate, "round", None)

        routing_logger.info(f"[VectorStore] 重建完成：共 {counter} 轮")

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

    def search(
        self,
        agent_name: str,
        query: str,
        limit: int | None = None,
        kind: str = "memory",
    ) -> list[dict[str, Any]]:
        """语义搜索：按 kind 在可见范围内检索。"""
        if not query or not query.strip():
            return []

        if not isinstance(limit, int) or limit <= 0:
            try:
                limit = int(os.getenv("VECTOR_SEARCH_LIMIT", "5"))
            except ValueError:
                limit = 5

        # 计算查询向量（同步）
        try:
            qvec = _embed_sync([query])[0]
        except Exception as e:
            routing_logger.error(f"[VectorStore] 查询嵌入失败: {e}")
            return []

        # 执行近邻搜索
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(DB_PATH)
            self._load_sqlite_vec_sync(conn)

            # sqlite-vec 要求 MATCH 必须有 LIMIT，所以先在 CTE 中搜更多候选
            candidate_limit = max(limit * 10, 50)  # 至少 50 个候选

            kind_norm = (kind or "memory").strip().lower()
            if kind_norm in ("round", "dialogue"):
                scope_sql = (
                    "SELECT id FROM chunks WHERE source = 'round' AND EXISTS ("
                    "SELECT 1 FROM json_each(chunks.visible_to) WHERE json_each.value = ?"
                    ")"
                )
                scope_params = (agent_name,)
            elif kind_norm == "all":
                scope_sql = (
                    "SELECT id FROM chunks WHERE ("
                    "source = 'memory' AND owner_agent = ?"
                    ") OR ("
                    "source = 'round' AND EXISTS ("
                    "SELECT 1 FROM json_each(chunks.visible_to) WHERE json_each.value = ?"
                    ")"
                    ")"
                )
                scope_params = (agent_name, agent_name)
            else:
                scope_sql = "SELECT id FROM chunks WHERE source = 'memory' AND owner_agent = ?"
                scope_params = (agent_name,)

            sql = f"""
            WITH scope AS (
              {scope_sql}
            ),
            vec_results AS (
              SELECT rowid, distance FROM vec_chunks
              WHERE embedding MATCH ?
              LIMIT ?
            )
            SELECT c.id, c.content, v.distance
            FROM vec_results v
            JOIN scope s ON s.id = v.rowid
            JOIN chunks c ON c.id = v.rowid
            ORDER BY v.distance
            LIMIT ?
            """
            rows = conn.execute(
                sql,
                (*scope_params, self._to_vec_blob(qvec), candidate_limit, limit),
            ).fetchall()

            routing_logger.info(
                "[VectorStore] 搜索完成: agent=%s, limit=%s, 命中=%s, kind=%s",
                agent_name, limit, len(rows), kind_norm
            )

            return [{"id": str(r[0]), "content": r[1], "score": float(r[2])} for r in rows]
        except Exception as e:
            routing_logger.error(f"[VectorStore] 检索失败: {e}")
            return []
        finally:
            if conn is not None:
                conn.close()

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
