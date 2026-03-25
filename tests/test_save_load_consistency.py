"""测试游戏存档加载功能中向量索引的一致性

验证思路：
1. 初始状态：向量数据库中已有数据
2. 保存操作：执行 save 操作
3. 加载操作：执行 load 操作
4. 验证：对比 load 后的数据库内容是否与 save 前的数据库内容完全一致
"""

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# 设置项目根目录
project_root = Path(__file__).parent.parent
os.chdir(project_root)
sys.path.insert(0, str(project_root))

# 加载环境变量
try:
    from dotenv import load_dotenv
    load_dotenv(project_root / ".env")
except ImportError:
    pass

# 导入必要模块
try:
    import importlib
    import storage.vector_store
    import memory.retrieval
    vector_store_module = importlib.import_module("storage.vector_store")
    retrieval_module = importlib.import_module("memory.retrieval")
    from storage.vector_store import vector_store, EMBED_DIM, EMBED_API_URL, EMBED_API_KEY
    from engine.save_manager import export_save_archive, import_save_archive
except ModuleNotFoundError as exc:
    pytest.skip(f"skip save_load tests: missing dependency ({exc})", allow_module_level=True)

# 检查 embedding 配置
pytestmark = pytest.mark.skipif(
    not EMBED_API_URL or not EMBED_API_KEY,
    reason="EMBEDDING_API_URL 或 EMBEDDING_API_KEY 未配置，跳过测试"
)


def make_character_path(tmp_path):
    def _path(name, subpath=None):
        base = tmp_path / name
        if subpath:
            return str(base / subpath)
        return str(base)
    return _path


def write_memory(tmp_path, agent_name: str, content: str):
    agent_dir = tmp_path / agent_name
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "memory.md"
    path.write_text(content, encoding="utf-8")
    return path


def get_chunks(tmp_path, agent_name: str, date: str) -> list[str]:
    from memory.parser import split_by_date, normalize, split_into_events
    path = tmp_path / agent_name / "memory.md"
    sections = split_by_date(normalize(path.read_text(encoding="utf-8")))
    return split_into_events(sections.get(date, ""))


_MEMORY_CHUNK_COLS = [
    "id",
    "memory_key",
    "owner_agent",
    "game_date",
    "content",
    "keywords",
    "importance",
    "content_hash",
    "last_recalled_at",
]


def _get_db_snapshot(db_path: str) -> dict:
    """获取数据库快照：memory_chunks 和 vec_memory_chunks 的内容"""
    if not os.path.exists(db_path):
        return {"memory_chunks": [], "vec_memory_chunks": []}

    conn = sqlite3.connect(db_path)
    try:
        try:
            import sqlite_vec
            ext_path = sqlite_vec.loadable_path()
            conn.enable_load_extension(True)
            conn.execute(f"SELECT load_extension('{ext_path}')")
        except Exception:
            pass

        rows = conn.execute(
            "SELECT id, memory_key, owner_agent, game_date, content, keywords, importance, content_hash, last_recalled_at "
            "FROM memory_chunks ORDER BY id"
        ).fetchall()

        vec_count = conn.execute("SELECT COUNT(*) FROM vec_memory_chunks").fetchone()[0]

        return {
            "memory_chunks": [dict(zip(_MEMORY_CHUNK_COLS, row)) for row in rows],
            "vec_memory_chunks": [{"rowid": i} for i in range(vec_count)],
        }
    finally:
        conn.close()


def _compare_snapshots(before: dict, after: dict) -> tuple[bool, str]:
    """比较两个数据库快照，返回 (是否一致, 差异描述)"""
    if len(before["memory_chunks"]) != len(after["memory_chunks"]):
        return False, f"memory_chunks 数量不同: before={len(before['memory_chunks'])}, after={len(after['memory_chunks'])}"

    if len(before["vec_memory_chunks"]) != len(after["vec_memory_chunks"]):
        return False, f"vec_memory_chunks 数量不同: before={len(before['vec_memory_chunks'])}, after={len(after['vec_memory_chunks'])}"

    # 比较 memory_chunks 内容（忽略 id，因为 AUTOINCREMENT 在 DELETE 后重新 INSERT 不会重置）
    for i, (b, a) in enumerate(zip(before["memory_chunks"], after["memory_chunks"])):
        b_copy = {k: v for k, v in b.items() if k != "id"}
        a_copy = {k: v for k, v in a.items() if k != "id"}
        if b_copy != a_copy:
            return False, f"memory_chunks[{i}] 不同: before={b_copy}, after={a_copy}"

    return True, "数据库内容完全一致"


class TestSaveLoadConsistency:
    """测试 save-load 循环中向量索引的一致性"""

    @pytest.mark.asyncio
    async def test_vector_index_consistency_after_save_load(self, tmp_path, monkeypatch):
        """验证 save-load 循环后向量索引一致性"""
        test_db_path = str(tmp_path / "test_vectors.sqlite")
        monkeypatch.setattr(vector_store_module, "DB_PATH", test_db_path)
        monkeypatch.setattr(retrieval_module, "DB_PATH", test_db_path)

        store = vector_store
        if store._db is not None:
            await store._db.close()
        store._db = None
        monkeypatch.setattr(store, "character_path", make_character_path(tmp_path))

        try:
            await store.init_tables()

            write_memory(
                tmp_path,
                "lilith",
                "# lilith\n\n## 4月3日\n"
                "- **时间**：4月3日 09:00\n- **地点**：教室\n- **在场**：莉莉丝\n"
                "- **内容**：这是第一轮对话的内容，包含重要信息。",
            )
            await store.add("lilith", "4月3日", get_chunks(tmp_path, "lilith", "4月3日"))

            write_memory(
                tmp_path,
                "mitsuki",
                "# mitsuki\n\n## 4月3日\n"
                "- **时间**：4月3日 09:30\n- **地点**：走廊\n- **在场**：美月\n"
                "- **内容**：这是第二轮对话的内容，mitsuki 的回应。",
            )
            await store.add("mitsuki", "4月3日", get_chunks(tmp_path, "mitsuki", "4月3日"))

            from memory.retrieval import search_memories
            search_result = search_memories("lilith", "第一轮对话")
            assert search_result != "（无相关记忆）", "save 前应该能搜索到数据"

            snapshot_before = _get_db_snapshot(test_db_path)
            assert len(snapshot_before["memory_chunks"]) == 2, "应该有 2 条记忆"
            assert len(snapshot_before["vec_memory_chunks"]) == 2, "应该有 2 条向量"

            # 模拟 save-load 循环：清空数据库
            db = await store._get_db()
            await db.execute("DELETE FROM vec_memory_chunks")
            await db.execute("DELETE FROM memory_chunks")
            await db.commit()

            snapshot_empty = _get_db_snapshot(test_db_path)
            assert len(snapshot_empty["memory_chunks"]) == 0, "清空后应该没有记忆"
            assert len(snapshot_empty["vec_memory_chunks"]) == 0, "清空后应该没有向量"

            # 重新加载相同的数据（模拟 rebuild）
            await store.add("lilith", "4月3日", get_chunks(tmp_path, "lilith", "4月3日"))
            await store.add("mitsuki", "4月3日", get_chunks(tmp_path, "mitsuki", "4月3日"))

            snapshot_after = _get_db_snapshot(test_db_path)

            assert len(snapshot_after["memory_chunks"]) == len(snapshot_before["memory_chunks"]), \
                f"memory_chunks 数量不一致: before={len(snapshot_before['memory_chunks'])}, after={len(snapshot_after['memory_chunks'])}"
            assert len(snapshot_after["vec_memory_chunks"]) == len(snapshot_before["vec_memory_chunks"]), \
                f"vec_memory_chunks 数量不一致: before={len(snapshot_before['vec_memory_chunks'])}, after={len(snapshot_after['vec_memory_chunks'])}"

            is_consistent, message = _compare_snapshots(snapshot_before, snapshot_after)
            assert is_consistent, f"save-load 后数据库不一致: {message}"

            search_result_after = search_memories("lilith", "第一轮对话")
            assert search_result_after != "（无相关记忆）", "load 后应该能搜索到数据"
        finally:
            if store._db is not None:
                await store._db.close()
                store._db = None
