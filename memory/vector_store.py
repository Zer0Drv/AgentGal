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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import httpx

from log_config.routing import routing_logger
from engine.config import character_path, PROJECT_ROOT
from memory.file_ops import load_consolidation_state
from memory.text_utils import normalize, split_by_date, split_into_events


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


def _utcnow_iso() -> str:
    """UTC ISO8601，尾部使用 Z。"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _extract_game_date(text: str) -> str | None:
    """从文本中提取游戏日期（如 4月3日）"""
    m = re.search(r"\*\*时间\*\*：\s*(\d{1,2}月\d{1,2}日)", text)
    if m:
        return m.group(1)
    return None


def _parse_cn_date(date_text: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"(\d{1,2})月(\d{1,2})日", (date_text or "").strip())
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    if 1 <= month <= 12 and 1 <= day <= 31:
        return month, day
    return None


def _is_date_before(date_text: str, cutoff_date: str) -> bool:
    left = _parse_cn_date(date_text)
    right = _parse_cn_date(cutoff_date)
    if left is None or right is None:
        return False
    return left < right


def _date_key(date_text: str) -> int | None:
    parsed = _parse_cn_date(date_text)
    if parsed is None:
        return None
    month, day = parsed
    return month * 100 + day


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
        self._memory_index_tasks: set[asyncio.Task] = set()
        self._memory_index_cutoff: dict[str, str] = {}
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

    def add_round(self, visible_to: list[str], round_id: str, content: str, game_date: str | None = None):
        """一轮对话结束时调用，后台写入数据库（不阻塞）。

        - 存储为"每轮一条" chunk，visible_to 决定可见范围。
        - 失败时仅记录日志，不抛异常影响主流程。
        """
        task = asyncio.create_task(self._do_add_round(visible_to, round_id, content, game_date))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _do_add_round(self, visible_to: list[str], round_id: str, content: str, game_date: str | None = None):
        """实际执行入库（内部方法）。"""
        # 解析会话 ID
        conv_id = round_id.rsplit("_", 1)[0]
        prev_game_date = self._conv_game_date.get(conv_id)

        # 统一可见性：调用方已保证含 narrator
        visible = list(dict.fromkeys(visible_to))

        # 解析或沿用"游戏日期"
        if game_date is None:
            game_date = _extract_game_date(content)
        if game_date:
            self._conv_game_date[conv_id] = game_date
        game_date = game_date or self._conv_game_date.get(conv_id, "")

        # 计算 embedding
        try:
            embeddings = await _embed_async([content])
            embedding = embeddings[0]
        except Exception as e:
            routing_logger.error(f"[VectorStore] 计算嵌入失败: round_id={round_id}, error={e}")
            return

        # 入库
        db: aiosqlite.Connection | None = None
        try:
            async with self._get_write_lock():
                await self.init_tables()
                db = await self._get_db()
                now_iso = _utcnow_iso()
                visible_json = json.dumps(visible, ensure_ascii=False)

                await db.execute("BEGIN")

                # 避免 INSERT OR REPLACE 触发 delete+insert 导致 rowid 变化，
                # 进而在 vec_chunks 残留旧向量行。这里改为显式 update/insert，保持 rowid 稳定。
                cur = await db.execute(
                    "SELECT id FROM chunks WHERE round_id = ?",
                    (round_id,),
                )
                existing = await cur.fetchone()
                if existing:
                    rowid = int(existing[0])
                    await db.execute(
                        """
                        UPDATE chunks
                        SET date = ?, created_at = ?, visible_to = ?, content = ?, source = 'round', owner_agent = NULL
                        WHERE id = ?
                        """,
                        (game_date, now_iso, visible_json, content, rowid),
                    )
                else:
                    cur = await db.execute(
                        """
                        INSERT INTO chunks(round_id, date, created_at, visible_to, content, source, owner_agent)
                        VALUES (?, ?, ?, ?, ?, 'round', NULL)
                        """,
                        (round_id, game_date, now_iso, visible_json, content),
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
            routing_logger.info(f"[VectorStore] 入库完成: round_id={round_id}")
            if prev_game_date and game_date and game_date != prev_game_date:
                self._trigger_memory_indexing(visible, game_date)
        except Exception as e:
            try:
                if db is not None:
                    await db.execute("ROLLBACK")
            except Exception:
                pass
            routing_logger.error(f"[VectorStore] 写入失败: round_id={round_id}, error={e}")

    def _trigger_memory_indexing(self, visible_to: list[str], game_date: str):
        for agent in list(dict.fromkeys(visible_to)):
            routing_logger.info(
                "[VectorStore] 触发长期记忆索引: agent=%s, game_date=%s",
                agent, game_date
            )
            task = asyncio.create_task(self._index_memory_before_date(agent, game_date))
            self._memory_index_tasks.add(task)
            task.add_done_callback(self._memory_index_tasks.discard)

    async def _index_memory_before_date(self, agent_name: str, fallback_cutoff: str):
        cutoff = load_consolidation_state(agent_name) or fallback_cutoff
        if not _parse_cn_date(cutoff):
            routing_logger.info(
                "[VectorStore] 跳过长期记忆索引: agent=%s, 无效cutoff=%s",
                agent_name, cutoff
            )
            return
        if self._memory_index_cutoff.get(agent_name) == cutoff:
            routing_logger.info(
                "[VectorStore] 跳过长期记忆索引: agent=%s, cutoff未变化=%s",
                agent_name, cutoff
            )
            return

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
        payloads: list[tuple[str, str, str]] = []
        for date, body in sections.items():
            if not _is_date_before(date, cutoff):
                continue
            events = split_into_events(body)
            for idx, event in enumerate(events, start=1):
                text = event.strip()
                if text:
                    payloads.append((f"memory::{agent_name}::{date}::{idx}", date, text))
        routing_logger.info(
            "[VectorStore] 开始长期记忆索引: agent=%s, cutoff=%s, 待写入事件=%s",
            agent_name, cutoff, len(payloads)
        )

        db: aiosqlite.Connection | None = None
        try:
            async with self._get_write_lock():
                await self.init_tables()
                db = await self._get_db()

                await db.execute("BEGIN")
                await db.execute(
                    "DELETE FROM vec_chunks WHERE rowid IN ("
                    "SELECT id FROM chunks WHERE source = 'memory' AND owner_agent = ?"
                    ")",
                    (agent_name,),
                )
                await db.execute(
                    "DELETE FROM chunks WHERE source = 'memory' AND owner_agent = ?",
                    (agent_name,),
                )

                if payloads:
                    embeddings = await _embed_async([item[2] for item in payloads])
                    visible_json = json.dumps([agent_name], ensure_ascii=False)
                    now_iso = _utcnow_iso()
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
                self._memory_index_cutoff[agent_name] = cutoff
                routing_logger.info(
                    "[VectorStore] 长期记忆索引完成: agent=%s, cutoff=%s, 写入事件=%s",
                    agent_name, cutoff, len(payloads)
                )
        except Exception as e:
            try:
                if db is not None:
                    await db.execute("ROLLBACK")
            except Exception:
                pass
            routing_logger.error(
                "[VectorStore] 索引长期记忆失败: agent=%s, cutoff=%s, error=%s",
                agent_name, cutoff, e
            )

    # ----------------------------- 重建 -----------------------------

    async def rebuild(self, agent_name: str):
        """最小化重建：解析 jsonl → 直接入库。"""
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
                gdate = _extract_game_date(str(m.get("content", "")))
                if gdate:
                    break
            return content, vis, gdate

        counter = 0
        for msgs in _iter_rounds():
            counter += 1
            content, vis, gdate = _format_round(msgs)
            round_id = f"rebuild_{counter}"
            await self._do_add_round(vis, round_id, content, gdate)

        routing_logger.info(f"[VectorStore] 重建完成：共 {counter} 轮")

    # ----------------------------- 检索 -----------------------------

    def search(self, agent_name: str, query: str, limit: int | None = None) -> list[dict[str, Any]]:
        """语义搜索：仅在"当前角色可见"的 chunks 中检索。"""
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
            cutoff = load_consolidation_state(agent_name)
            cutoff_is_valid = bool(_parse_cn_date(cutoff or ""))
            cutoff_key = _date_key(cutoff or "") or -1
            routing_logger.info(
                "[VectorStore] 搜索范围: agent=%s, cutoff=%s, cutoff_valid=%s",
                agent_name, cutoff, cutoff_is_valid
            )

            # 策略：先扩大候选集搜索，再精确过滤
            # sqlite-vec 要求 MATCH 必须有 LIMIT，所以先在 CTE 中搜更多候选
            candidate_limit = max(limit * 10, 50)  # 至少 50 个候选

            rows = conn.execute(
                """
                WITH scope AS (
                  SELECT id
                  FROM chunks
                  WHERE (
                    source = 'round'
                    AND EXISTS (
                      SELECT 1 FROM json_each(chunks.visible_to)
                      WHERE json_each.value = ?
                    )
                  ) OR (
                    source = 'memory'
                    AND owner_agent = ?
                    AND ? = 1
                    AND instr(date, '月') > 1
                    AND instr(date, '日') > instr(date, '月')
                    AND (
                      CAST(substr(date, 1, instr(date, '月') - 1) AS INTEGER) * 100
                      + CAST(substr(
                          date,
                          instr(date, '月') + 1,
                          instr(date, '日') - instr(date, '月') - 1
                        ) AS INTEGER)
                    ) < ?
                  )
                ),
                vec_results AS (
                  SELECT rowid, distance FROM vec_chunks
                  WHERE embedding MATCH ?
                  LIMIT ?
                )
                SELECT c.id, c.content, v.distance, c.source
                FROM vec_results v
                JOIN scope s ON s.id = v.rowid
                JOIN chunks c ON c.id = v.rowid
                ORDER BY v.distance
                LIMIT ?
                """,
                (
                    agent_name,
                    agent_name,
                    1 if cutoff_is_valid else 0,
                    cutoff_key,
                    self._to_vec_blob(qvec),
                    candidate_limit,
                    limit,
                ),
            ).fetchall()

            round_hits = sum(1 for r in rows if len(r) > 3 and r[3] == "round")
            memory_hits = sum(1 for r in rows if len(r) > 3 and r[3] == "memory")

            routing_logger.info(
                "[VectorStore] 搜索完成: agent=%s, limit=%s, 命中=%s(round=%s,memory=%s)",
                agent_name, limit, len(rows), round_hits, memory_hits
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
        results = await asyncio.gather(*(self.delete(name) for name in agent_names))
        return dict(zip(agent_names, results))


# 全局实例
vector_store = VectorStore()
