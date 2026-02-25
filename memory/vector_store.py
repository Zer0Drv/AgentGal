"""本地向量存储（sqlite-vec）

- 每 10 轮对话索引一次（每轮 = 从一条玩家消息到下一条玩家消息之间）。
- 每轮作为一个 chunk 入库；为该轮所有可见角色各写一条（方便按可见性过滤）。
- 检索时仅在当前角色可见的 chunks 中做 ANN 检索。
"""

from __future__ import annotations

import os
import json
import asyncio
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from log_config.routing import routing_logger
from engine.config import character_path, PROJECT_ROOT


# ----------------------------- 配置与常量 -----------------------------

DB_PATH = str(PROJECT_ROOT / "data" / "vectors.sqlite")

# 默认使用 OpenAI 兼容 Embeddings 接口；兼容 EMBEDDING_MODEL 与 EMBEDDING_MODEL_ID 两种变量名
EMBED_MODEL = os.getenv("EMBEDDING_MODEL_ID") or os.getenv("EMBEDDING_MODEL") or "text-embedding-3-small"
EMBED_API_KEY = os.getenv("EMBEDDING_API_KEY") or os.getenv("LLM_API_KEY", "")
EMBED_API_URL = os.getenv("EMBEDDING_API_URL") or os.getenv("LLM_API_URL")

# 维度：根据模型选择，text-embedding-3-small=1536；兼容 .env 的 EMBEDDING_DIM
EMBED_DIM = int(os.getenv("EMBEDDING_DIM", "1536"))

# 每多少轮触发一次索引（批量嵌入+落库）。优先 VECTOR_INDEX_INTERVAL，其次 INDEX_INTERVAL，默认 10。
INDEX_INTERVAL = int(os.getenv("VECTOR_INDEX_INTERVAL") or os.getenv("INDEX_INTERVAL") or "10")


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


@dataclass
class _Round:
    """待索引的单轮对话"""

    round_id: str
    visible_to: list[str]
    content: str
    date: str  # 游戏内日期，如 "4月3日"（不含具体时间）


class _EmbeddingClient:
    """极薄封装：OpenAI 兼容 Embeddings 客户端（同步 & 异步两用）。"""

    def __init__(self):
        self._sync_client = None
        self._async_client = None

    # 同步
    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        try:
            from openai import OpenAI

            if not EMBED_API_KEY:
                raise ValueError(
                    "EMBEDDING_API_KEY 或 LLM_API_KEY 未配置，无法计算向量"
                )
            if self._sync_client is None:
                self._sync_client = OpenAI(api_key=EMBED_API_KEY, base_url=EMBED_API_URL)

            resp = self._sync_client.embeddings.create(model=EMBED_MODEL, input=texts)
            return [d.embedding for d in resp.data]
        except Exception as e:
            raise RuntimeError(f"计算嵌入失败: {e}")

    # 异步
    async def embed_async(self, texts: list[str]) -> list[list[float]]:
        try:
            from openai import AsyncOpenAI

            if not EMBED_API_KEY:
                raise ValueError(
                    "EMBEDDING_API_KEY 或 LLM_API_KEY 未配置，无法计算向量"
                )
            if self._async_client is None:
                self._async_client = AsyncOpenAI(
                    api_key=EMBED_API_KEY, base_url=EMBED_API_URL
                )

            resp = await self._async_client.embeddings.create(
                model=EMBED_MODEL, input=texts
            )
            return [d.embedding for d in resp.data]
        except Exception as e:
            raise RuntimeError(f"计算嵌入失败: {e}")

    async def close(self):
        """关闭所有 OpenAI 客户端，释放资源。"""
        if self._sync_client is not None:
            try:
                self._sync_client.close()
            except Exception:
                pass
            finally:
                self._sync_client = None
        
        if self._async_client is not None:
            try:
                await self._async_client.close()
            except Exception:
                pass
            finally:
                self._async_client = None


_embedder = _EmbeddingClient()


class VectorStore:
    """sqlite-vec 本地向量库。

    表结构：
    - chunks(id INTEGER PK, round_id TEXT UNIQUE, date TEXT, visible_to TEXT, content TEXT)
    - vec_chunks USING vec0(embedding F32[EMBED_DIM])  -- rowid 对应 chunks.id
    - sync_state(scope TEXT PRIMARY KEY, sync_offset INTEGER, content_hash TEXT)  # 预留给外部工具
    """

    def __init__(self):
        self._db: aiosqlite.Connection | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        # 允许测试替换 embedder
        self._embedder = None
        # 允许测试替换 character_path
        self.character_path = character_path
        # 轮次缓冲：按 conversation_id 聚合，触发每 10 轮批量入库
        self._pending: dict[str, list[_Round]] = {}
        # 每个会话已落库的轮次数（偏移）
        self._flushed_count: dict[str, int] = {}
        # 记录每个会话最近一次解析到的游戏日期（如 "4月3日"）
        self._conv_game_date: dict[str, str] = {}
        # 追踪所有创建的后台任务
        self._pending_tasks: set[asyncio.Task] = set()
        # 标志位：已关闭，禁止创建新任务
        self._closing = False

    # ----------------------------- DB 基础 -----------------------------

    def _get_lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def _load_sqlite_vec(self, conn: aiosqlite.Connection):
        """在当前连接加载 sqlite-vec 扩展（重复调用安全）。"""
        try:
            import sqlite_vec  # type: ignore

            # 获取扩展库的完整路径，通过 SQL 加载（避免线程问题）
            ext_path = sqlite_vec.loadable_path()
            await conn.enable_load_extension(True)
            await conn.execute(f"SELECT load_extension('{ext_path}')")
        except Exception as e:
            raise RuntimeError(f"加载 sqlite-vec 扩展失败，请安装 sqlite_vec: {e}")

    async def _get_db(self) -> aiosqlite.Connection:
        if self._db is None:
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
            self._db = await aiosqlite.connect(DB_PATH)
            await self._db.execute("PRAGMA journal_mode=WAL;")
            await self._load_sqlite_vec(self._db)
        return self._db

    def _get_embedder(self):
        return self._embedder or _embedder

    async def init_tables(self):
        db = await self._get_db()
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id TEXT NOT NULL UNIQUE,
                date TEXT,                        -- 游戏内日期（如 4月3日）
                created_at TEXT,                  -- 写入时间（UTC ISO8601）
                visible_to TEXT,                  -- JSON array of agent names
                content TEXT NOT NULL
            )
            """
        )
        # 兼容旧表结构：确保 round_id 唯一约束存在
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_round_id ON chunks(round_id)"
        )
        await db.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
                embedding F32[{EMBED_DIM}]
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_state (
                scope TEXT PRIMARY KEY,
                sync_offset INTEGER NOT NULL DEFAULT 0,
                content_hash TEXT
            )
            """
        )
        # 迁移：为已有表补充缺失列（created_at）
        try:
            cur = await db.execute("PRAGMA table_info(chunks)")
            cols = [row[1] for row in await cur.fetchall()]
            if "created_at" not in cols:
                await db.execute("ALTER TABLE chunks ADD COLUMN created_at TEXT")
        except Exception:
            pass

        await db.commit()

    # ----------------------------- 写入路径 -----------------------------

    def add_round(self, visible_to: list[str], round_id: str, content: str, game_date: str | None = None):
        """一轮对话结束时调用（对外接口）。

        - 先入内存缓冲；达到 INDEX_INTERVAL 时批量嵌入+落库。
        - 存储为“每轮一条” chunk，visible_to 决定可见范围。
        """

        # round_id 形如 "{conversation_id}_{counter}" → 解析会话与计数
        conv_id = round_id.rsplit("_", 1)[0]
        count_str = round_id.split("_")[-1]
        try:
            counter = int(count_str)
        except ValueError:
            counter = -1

        # 统一可见性：调用方已保证含 narrator
        visible = list(dict.fromkeys(visible_to))

        # 解析或沿用“游戏日期”（如 4月3日）
        def _extract_game_date(text: str) -> str | None:
            import re
            # 匹配类似：**时间**：4月3日 08:20 / **时间**：4月3日
            m = re.search(r"\*\*时间\*\*：\s*(\d{1,2}月\d{1,2}日)", text)
            if m:
                return m.group(1)
            return None

        if game_date is None:
            game_date = _extract_game_date(content)
        if game_date:
            self._conv_game_date[conv_id] = game_date
        game_date = game_date or self._conv_game_date.get(conv_id, "")

        entry = _Round(
            round_id=round_id,
            visible_to=visible,
            content=content,
            date=game_date,
        )

        if conv_id not in self._pending:
            self._pending[conv_id] = []
            self._flushed_count[conv_id] = 0
        # 去重：同一 round_id 只保留最后一次写入
        pending = self._pending[conv_id]
        for i, existing in enumerate(pending):
            if existing.round_id == round_id:
                pending[i] = entry
                break
        else:
            pending.append(entry)

        # 达到 INDEX_INTERVAL 触发批量入库（禁止 closing 状态下创建任务）
        if not self._closing and counter > 0 and INDEX_INTERVAL > 0 and counter % INDEX_INTERVAL == 0:
            task = asyncio.create_task(self._flush_new_chunks(conv_id, batch_size=INDEX_INTERVAL))
            # 记录任务，以便 close() 时等待完成
            self._pending_tasks.add(task)
            # 任务完成时自动移除
            task.add_done_callback(self._pending_tasks.discard)

    async def _flush_new_chunks(self, conv_id: str, batch_size: int | None = None):
        """将该会话自上次落库后的新轮次写入 sqlite-vec。

        Args:
            conv_id: 会话 ID（从 round_id 前缀推导）
            batch_size: 本次最多写入多少轮；None 表示全部写入。
        """
        # 兼容测试将 round_id 直接传入
        if conv_id not in self._pending and "_" in conv_id:
            derived = conv_id.rsplit("_", 1)[0]
            if derived in self._pending:
                conv_id = derived

        lock = self._get_lock(conv_id)
        print(f"[_flush_new_chunks] 开始: conv_id={conv_id}, batch_size={batch_size}")
        async with lock:
            rounds = self._pending.get(conv_id, [])
            print(f"[_flush_new_chunks] 获取锁: rounds={len(rounds)}")
            if not rounds:
                print("[_flush_new_chunks] rounds为空，返回")
                return

            start = self._flushed_count.get(conv_id, 0)
            end = len(rounds) if batch_size is None else min(len(rounds), start + batch_size)
            print(f"[_flush_new_chunks] start={start}, end={end}")
            if end <= start:
                print("[_flush_new_chunks] end<=start，返回")
                return

            batch = rounds[start:end]
            texts: list[str] = []
            meta: list[tuple[str, str, str, str, str]] = []
            # meta: (round_id, date, created_at, visible_json, content)

            # 每轮只写一条记录（包含完整 visible_to）
            now_iso = datetime.utcnow().isoformat() + "Z"
            for rd in batch:
                texts.append(rd.content)
                meta.append(
                    (
                        rd.round_id,
                        rd.date,
                        now_iso,
                        json.dumps(rd.visible_to, ensure_ascii=False),
                        rd.content,
                    )
                )

            # 计算 embedding（异步批量）
            try:
                embeddings = await self._get_embedder().embed_async(texts)
            except Exception as e:
                routing_logger.error(f"[VectorStore] 计算嵌入失败，批量写入中止: {e}")
                return

            print(f"[VectorStore] 计算嵌入成功: {len(embeddings)} 条，维度 {len(embeddings[0]) if embeddings else 0}")

            # 入库
            await self.init_tables()
            db = await self._get_db()
            await db.execute("BEGIN")
            try:
                # 插入 chunks，并收集 rowid+embedding（用 INSERT OR IGNORE 避免重复轮次）
                inserted: list[tuple[int, list[float]]] = []
                for (rid, date, created_at, vis, content), emb in zip(meta, embeddings):
                    cur = await db.execute(
                        """
                        INSERT OR IGNORE INTO chunks(round_id, date, created_at, visible_to, content)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (rid, date, created_at, vis, content),
                    )
                    if cur.lastrowid:
                        inserted.append((cur.lastrowid, emb))
                        print(f"[VectorStore] 插入 chunk: rid={rid}, rowid={cur.lastrowid}")
                    else:
                        print(f"[VectorStore] chunk 已存在或插入失败: rid={rid}")

                print(f"[VectorStore] 成功插入 {len(inserted)} 个 chunks，准备插入向量")

                # 插入向量（vec_chunks.rowid = 对应 chunks.id）
                # 注意：只插入实际成功插入 chunks 的向量
                if inserted:
                    vec_data: list[tuple[int, bytes]] = []
                    for rowid, emb in inserted:
                        blob = self._to_vec_blob(emb)
                        vec_data.append(
                            (rowid, blob)
                        )
                    print(f"[VectorStore] 插入向量: {len(vec_data)} 条")
                    await db.executemany(
                        "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
                        vec_data,
                    )

                # 更新 sync_state（以 conv_id 为 scope）
                await db.execute(
                    """
                    INSERT INTO sync_state(scope, sync_offset, content_hash)
                    VALUES(?, ?, ?)
                    ON CONFLICT(scope) DO UPDATE SET
                      sync_offset=excluded.sync_offset,
                      content_hash=excluded.content_hash
                    """,
                    (conv_id, end, _sha1(meta[-1][0])),
                )

                await db.commit()
                print(f"[VectorStore] 事务已提交: {conv_id}, {end - start} 轮")
            except Exception as e:
                await db.execute("ROLLBACK")
                print(f"[VectorStore] 批量写入失败: {e}")
                return

            # 更新偏移并适当裁剪内存占用
            print(f"[VectorStore] 更新 flushed_count: {conv_id} = {end}")
            self._flushed_count[conv_id] = end
            # 当缓存过大时，丢弃已落库的前缀，保留未落库的尾部
            if end > 0 and end == len(rounds):
                # 全部落库，直接清空
                self._pending[conv_id] = []
                self._flushed_count[conv_id] = 0
            elif end > 0 and end - start >= 10 and end > 20:
                # 部分落库且已很长，保留最后 20 轮
                self._pending[conv_id] = self._pending[conv_id][-20:]
                # 修正 flushed_count 到新的切片位置
                self._flushed_count[conv_id] = min(self._flushed_count[conv_id], len(self._pending[conv_id]))

    # 已不需要顺序 chunk_index（以 round_id 去重）

    # ----------------------------- 重建 -----------------------------

    async def rebuild(self, agent_name: str):
        """最小化重建：解析 jsonl → 切轮 → schedule_add_round。

        - 清空现有索引；
        - 将所有轮以 conv_id=rebuild、round_id=rebuild_{i} 回放；
        - 直接调用 _flush_new_chunks 而不创建后台任务。
        """
        import glob, re

        await self.init_tables()
        db = await self._get_db()

        # 清空
        await db.execute("DELETE FROM vec_chunks")
        await db.execute("DELETE FROM chunks")
        await db.execute("DELETE FROM sync_state")
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
                mm = re.search(r"\*\*时间\*\*：\s*(\d{1,2}月\d{1,2}日)", str(m.get("content", "")))
                if mm:
                    gdate = mm.group(1)
                    break
            return content, vis, gdate

        # 收集所有轮次，而不立即创建任务
        conv_id = "rebuild"
        counter = 0
        for msgs in _iter_rounds():
            counter += 1
            content, vis, gdate = _format_round(msgs)
            round_id = f"{conv_id}_{counter}"
            
            # 记录游戏日期
            if gdate:
                self._conv_game_date[conv_id] = gdate
            gdate = gdate or self._conv_game_date.get(conv_id, "")
            
            # 将轮次直接加入缓冲，不创建后台任务
            entry = _Round(
                round_id=round_id,
                visible_to=vis,
                content=content,
                date=gdate,
            )
            if conv_id not in self._pending:
                self._pending[conv_id] = []
                self._flushed_count[conv_id] = 0
            self._pending[conv_id].append(entry)

        # rebuild 结束后，一次性 flush 所有数据
        await self._flush_new_chunks(conv_id, batch_size=None)
        routing_logger.info(f"[VectorStore] 重建完成：共 {counter} 轮")

    # ----------------------------- 检索 -----------------------------

    def search(self, agent_name: str, query: str, limit: int | None = None) -> list[dict[str, Any]]:
        """语义搜索：仅在“当前角色可见”的 chunks 中检索。

        实现细节：我们为每个可见角色都写了一份 chunk，因此只需限制 agent_name。
        """
        if not query or not query.strip():
            return []
        # 默认 limit 读取环境变量 VECTOR_SEARCH_LIMIT（若未传入）
        if not isinstance(limit, int) or limit <= 0:
            try:
                limit = int(os.getenv("VECTOR_SEARCH_LIMIT", "5"))
            except ValueError:
                limit = 5

        # 1) 计算查询向量
        try:
            qvec = self._get_embedder().embed_sync([query])[0]
        except Exception as e:
            routing_logger.error(f"[VectorStore] 查询嵌入失败: {e}")
            return []

        # 2) 执行近邻搜索并连接内容（仅根据 visible_to 过滤）
        try:
            conn = sqlite3.connect(DB_PATH)
            # 加载扩展
            try:
                import sqlite_vec  # type: ignore

                ext_path = sqlite_vec.loadable_path()
                conn.enable_load_extension(True)
                conn.execute(f"SELECT load_extension('{ext_path}')")
            except Exception as e:
                raise RuntimeError(f"加载 sqlite-vec 扩展失败: {e}")

            # 限定当前 agent 可见（我们逐 agent 入库，天然满足可见性）
            # 用子查询限制搜索集合，避免跨 agent 泄露
            # 注意：vec0 虚拟表必须在 WHERE 中直接使用 LIMIT，不能通过 JOIN 来使用
            # 因此先从虚拟表查询，再 JOIN chunks 和 scope
            rows = conn.execute(
                """
                WITH scope AS (
                  SELECT id
                  FROM chunks
                  WHERE EXISTS (
                    SELECT 1 FROM json_each(chunks.visible_to)
                    WHERE json_each.value = ?
                  )
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
                """,
                (agent_name, self._to_vec_blob(qvec), limit),
            ).fetchall()

            routing_logger.info(
                "[VectorStore] 搜索完成: agent=%s, limit=%s, 命中=%s",
                agent_name,
                limit,
                len(rows),
            )

            out = [
                {"id": str(r[0]), "content": r[1], "score": float(r[2])}
                for r in rows
            ]
            return out
        except Exception as e:
            routing_logger.error(f"[VectorStore] 检索失败: {e}")
            return []
        finally:
            conn.close()

    # sqlite-vec 需要 BLOB 格式输入；python 包会在 SQL 边界处理，
    # 但我们在同步连接里手动处理以兼容某些平台。
    @staticmethod
    def _to_vec_blob(vec: Iterable[float]) -> bytes:
        import array

        a = array.array("f", [float(x) for x in vec])
        return a.tobytes()

    # ----------------------------- 删除/清理 -----------------------------

    async def delete(self, agent_name: str) -> bool:
        """删除指定 agent 的所有向量与文本 chunk。"""
        try:
            await self.init_tables()
            db = await self._get_db()
            # 删除与该角色相关可见性的所有记录（按 visible_to 语义）
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
            await db.execute("DELETE FROM sync_state WHERE scope IN (?, ?)", (agent_name, f"rebuild:{agent_name}"))
            await db.commit()
            routing_logger.info(f"[VectorStore] 已清空 {agent_name} 的向量与 chunks")
            return True
        except Exception as e:
            routing_logger.error(f"[VectorStore] 删除 {agent_name} 失败: {e}")
            return False

    async def delete_all_agents(self, agent_names: list[str]) -> dict[str, bool]:
        results = await asyncio.gather(*(self.delete(name) for name in agent_names))
        return dict(zip(agent_names, results))

    @staticmethod
    def _sync_cleanup():
        """在 atexit 中同步清理（轻量级，不等待任何异步操作）。
        
        策略：
        - 仅取消后台任务和清空缓冲
        - 不进行 flush（flush 依赖 asyncio，容易卡住）
        - 不关闭数据库（让垃圾回收处理）
        - 所有持久化的数据都在数据库中，丢弃缓冲不影响
        """
        try:
            # 1) 取消所有后台任务
            for task in vector_store._pending_tasks:
                if not task.done():
                    task.cancel()
            vector_store._pending_tasks.clear()
        except Exception:
            pass
        
        # 2) 清空待 flush 缓冲（数据库中的数据保留）
        try:
            vector_store._pending.clear()
            vector_store._flushed_count.clear()
        except Exception:
            pass



    async def close(self):
        """公开方法：可选的显式清理（应用通常无需调用）。
        
        用于需要手动控制清理时机的场景（如某些复杂应用）。
        大部分应用都会通过 atexit 钩子自动清理。
        """
        # 1) 设置关闭标志，禁止新任务创建
        self._closing = True
        
        # 2) 取消所有未完成的后台任务
        for task in self._pending_tasks:
            if not task.done():
                task.cancel()
        
        # 3) 短暂等待任务完成，超时则放弃
        try:
            if self._pending_tasks:
                await asyncio.wait_for(
                    asyncio.gather(*self._pending_tasks, return_exceptions=True),
                    timeout=1.0
                )
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass
        finally:
            self._pending_tasks.clear()
        
        # 4) 快速 flush（1秒超时）
        try:
            for conv_id in list(self._pending.keys()):
                try:
                    await asyncio.wait_for(
                        self._flush_new_chunks(conv_id, batch_size=None),
                        timeout=1.0
                    )
                except (asyncio.TimeoutError, Exception):
                    pass
        except Exception:
            pass
        
        # 5) 清空缓冲
        self._pending.clear()
        self._flushed_count.clear()
        
        # 6) 关闭数据库连接
        if self._db is not None:
            try:
                await self._db.close()
            except Exception:
                pass
            finally:
                self._db = None
        
        # 7) 关闭 OpenAI 客户端
        try:
            embedder = self._get_embedder()
            close_fn = getattr(embedder, "close", None)
            if close_fn:
                res = close_fn()
                if asyncio.iscoroutine(res):
                    await res
        except Exception:
            pass


# 全局实例
vector_store = VectorStore()

# 注意：atexit 自动清理已禁用，避免与事件循环冲突
# 应用应在适当时刻调用 await vector_store.close()
# 或使用 contextlib.asynccontextmanager 进行资源管理
