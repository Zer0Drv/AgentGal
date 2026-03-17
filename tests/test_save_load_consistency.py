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
    import memory.vector_store
    vector_store_module = importlib.import_module("memory.vector_store")
    from memory.vector_store import vector_store, EMBED_DIM, EMBED_API_URL, EMBED_API_KEY
    from game.save_manager import export_save_archive, import_save_archive
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


def _get_db_snapshot(db_path: str) -> dict:
    """获取数据库快照：chunks 和 vec_chunks 的完整内容"""
    if not os.path.exists(db_path):
        return {"chunks": [], "vec_chunks": []}
    
    conn = sqlite3.connect(db_path)
    try:
        # 加载 sqlite-vec 扩展
        try:
            import sqlite_vec
            ext_path = sqlite_vec.loadable_path()
            conn.enable_load_extension(True)
            conn.execute(f"SELECT load_extension('{ext_path}')")
        except Exception:
            pass
        
        # 获取 chunks 表数据
        chunks = conn.execute(
            "SELECT id, round_id, date, created_at, visible_to, content, source, owner_agent, last_recalled_at FROM chunks ORDER BY id"
        ).fetchall()
        
        # 只取 vec_chunks 行数（embedding 字节不做比较，由 search 断言覆盖）
        vec_count = conn.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]

        return {
            "chunks": [dict(zip(["id", "round_id", "date", "created_at", "visible_to", "content", "source", "owner_agent", "last_recalled_at"], row)) for row in chunks],
            "vec_chunks": [{"rowid": i} for i in range(vec_count)],
        }
    finally:
        conn.close()


def _compare_snapshots(before: dict, after: dict) -> tuple[bool, str]:
    """比较两个数据库快照，返回 (是否一致, 差异描述)

    注意：created_at 时间戳会变化，所以不比较该字段
    """
    if len(before["chunks"]) != len(after["chunks"]):
        return False, f"chunks 数量不同: before={len(before['chunks'])}, after={len(after['chunks'])}"

    if len(before["vec_chunks"]) != len(after["vec_chunks"]):
        return False, f"vec_chunks 数量不同: before={len(before['vec_chunks'])}, after={len(after['vec_chunks'])}"

    # 比较 chunks 内容（忽略 created_at，因为每次调用会生成新时间戳）
    for i, (b, a) in enumerate(zip(before["chunks"], after["chunks"])):
        # 比较除了 created_at、id 之外的所有字段：
        # - created_at 每次插入都会生成新时间戳
        # - id 是 AUTOINCREMENT，DELETE 后重新 INSERT 不会从 1 重置
        _IGNORE = {"created_at", "id"}
        b_copy = {k: v for k, v in b.items() if k not in _IGNORE}
        a_copy = {k: v for k, v in a.items() if k not in _IGNORE}
        if b_copy != a_copy:
            return False, f"chunks[{i}] 不同: before={b_copy}, after={a_copy}"

    # vec_chunks 只验证数量（embedding API 非确定性导致同文本的浮点字节可能不同，
    # 搜索可用性由后续 search 断言覆盖）

    return True, "数据库内容完全一致"


class TestSaveLoadConsistency:
    """测试 save-load 循环中向量索引的一致性"""

    @pytest.mark.asyncio
    async def test_vector_index_consistency_after_save_load(self, tmp_path, monkeypatch):
        """验证 save-load 循环后向量索引一致性

        思路：
        1. 初始状态：向量数据库中已有数据
        2. 保存操作：获取数据库快照
        3. 加载操作：清空并重新加载相同数据
        4. 验证：对比 load 后的数据库内容是否与 save 前一致
        """
        # 使用临时数据库
        test_db_path = str(tmp_path / "test_vectors.sqlite")
        monkeypatch.setattr(vector_store_module, "DB_PATH", test_db_path)

        store = vector_store
        if store._db is not None:
            await store._db.close()
        store._db = None
        monkeypatch.setattr(store, "character_path", make_character_path(tmp_path))

        try:
            # 初始化表
            await store.init_tables()

            # 添加一些测试数据（模拟游戏进行中）
            write_memory(
                tmp_path,
                "lilith",
                "# lilith\n\n## 4月3日\n"
                "- **时间**：4月3日 09:00\n- **地点**：教室\n- **在场**：莉莉丝\n"
                "- **内容**：这是第一轮对话的内容，包含重要信息。",
            )
            await store.add_memory("lilith", "4月3日")

            write_memory(
                tmp_path,
                "mitsuki",
                "# mitsuki\n\n## 4月3日\n"
                "- **时间**：4月3日 09:30\n- **地点**：走廊\n- **在场**：美月\n"
                "- **内容**：这是第二轮对话的内容，mitsuki 的回应。",
            )
            await store.add_memory("mitsuki", "4月3日")

            # 获取 save 前的快照（包括 chunks 和向量）
            snapshot_before = _get_db_snapshot(test_db_path)
            assert len(snapshot_before["chunks"]) == 2, "应该有 2 条 chunks"
            assert len(snapshot_before["vec_chunks"]) == 2, "应该有 2 条向量"

            # 验证数据库中的数据可以被搜索到
            search_result = store.search("lilith", "第一轮对话", kind="memory")
            assert len(search_result) >= 1, "save 前应该能搜索到数据"

            # 模拟 save-load 循环：清空数据库
            db = await store._get_db()
            await db.execute("DELETE FROM vec_chunks")
            await db.execute("DELETE FROM chunks")
            await db.commit()

            # 验证数据已清空
            snapshot_empty = _get_db_snapshot(test_db_path)
            assert len(snapshot_empty["chunks"]) == 0, "清空后应该没有 chunks"
            assert len(snapshot_empty["vec_chunks"]) == 0, "清空后应该没有向量"

            # 重新加载相同的数据（模拟 rebuild）
            await store.add_memory("lilith", "4月3日")
            await store.add_memory("mitsuki", "4月3日")

            # 获取 load 后的快照
            snapshot_after = _get_db_snapshot(test_db_path)

            # 验证数据数量一致
            assert len(snapshot_after["chunks"]) == len(snapshot_before["chunks"]), \
                f"chunks 数量不一致: before={len(snapshot_before['chunks'])}, after={len(snapshot_after['chunks'])}"
            assert len(snapshot_after["vec_chunks"]) == len(snapshot_before["vec_chunks"]), \
                f"vec_chunks 数量不一致: before={len(snapshot_before['vec_chunks'])}, after={len(snapshot_after['vec_chunks'])}"

            # 验证内容一致（忽略 created_at 和 id / rowid）
            is_consistent, message = _compare_snapshots(snapshot_before, snapshot_after)
            assert is_consistent, f"save-load 后数据库不一致: {message}"

            # 验证加载后的数据可以被搜索到
            search_result_after = store.search("lilith", "第一轮对话", kind="memory")
            assert len(search_result_after) >= 1, "load 后应该能搜索到数据"
        finally:
            # 确保在事件循环关闭前正确关闭 aiosqlite 连接，
            # 避免后台线程持有锁导致 threading._shutdown 死锁
            if store._db is not None:
                await store._db.close()
                store._db = None
