"""
Memory indexing tests

Focus: public interfaces only.
"""

import os
import sys
from pathlib import Path

import asyncio
import pytest
import pytest_asyncio

# 设置项目根目录，确保相对路径与模块导入一致
project_root = Path(__file__).parent.parent
os.chdir(project_root)
sys.path.insert(0, str(project_root))

# 必须在导入 vector_store 之前加载 .env
try:
    from dotenv import load_dotenv
    load_dotenv(project_root / ".env")
except ImportError:
    pass

try:
    from memory import vector_store as vs_module
    from memory.vector_store import VectorStore, EMBED_API_URL, EMBED_API_KEY
except ModuleNotFoundError as exc:
    pytest.skip(f"skip vector_store tests: missing dependency ({exc})", allow_module_level=True)


pytestmark = pytest.mark.skipif(
    not EMBED_API_URL or not EMBED_API_KEY,
    reason="EMBEDDING_API_URL 或 EMBEDDING_API_KEY 未配置，跳过测试",
)


@pytest_asyncio.fixture(scope="function")
async def store(tmp_path, monkeypatch):
    """为每个测试提供独立的 VectorStore（使用临时 DB）。"""
    db_path = str(tmp_path / "test_vectors.sqlite")
    monkeypatch.setattr(vs_module, "DB_PATH", db_path)

    store = VectorStore()
    await store.init_tables()
    return store


class TestVectorStoreMemoryIndexing:
    @pytest.mark.asyncio
    async def test_search_kind_memory_excludes_dialogue(self, store):
        """只检索 memory 时，不应返回仅有的对话数据。"""
        # 只写入对话
        store.add(
            ["mitsuki"],
            "dialogue_1",
            "玩家: 你好\n旁白: **时间**：4月3日 08:00\nmitsuki: 早上好",
            kind="dialogue",
        )
        # 等待后台写入完成（不使用私有接口）
        for _ in range(50):
            await asyncio.sleep(0.05)
            res = store.search("mitsuki", "早上好", kind="dialogue")
            if res:
                break

        res_mem = store.search("mitsuki", "早上好", kind="memory")
        assert res_mem == [], "仅有对话时，kind=memory 应该返回空"

    @pytest.mark.asyncio
    async def test_index_memory_date_splits_events(self, store, tmp_path, monkeypatch):
        """索引指定日期的 memory.md，按事件块拆分并可检索。"""
        # 构造 memory.md（同一天两个事件）
        mem_dir = tmp_path / "mitsuki"
        mem_dir.mkdir(parents=True)
        mem_path = mem_dir / "memory.md"
        mem_path.write_text(
            """
# mitsuki 的长期记忆

## 4月3日
- **时间**：4月3日 上午 08:18 - 08:52
- **地点**：教室
- **在场**：桥本美月、玩家（小明）、莉莉丝（转学生）
- **内容**：转学生莉莉丝到来，她的眼神让我在意。

- **时间**：4月3日 中午 12:00 - 12:30
- **地点**：走廊
- **在场**：桥本美月、玩家（小明）
- **内容**：我找他确认下午的安排。
""".strip(),
            encoding="utf-8",
        )

        def mock_character_path(name, subpath=None):
            base = tmp_path / name
            if subpath:
                return str(base / subpath)
            return str(base)

        monkeypatch.setattr(store, "character_path", mock_character_path)

        await store.index_memory_date("mitsuki", "4月3日")

        res1 = store.search("mitsuki", "眼神让我在意", kind="memory")
        res2 = store.search("mitsuki", "下午的安排", kind="memory")
        assert len(res1) >= 1, "应命中第一个事件块"
        assert len(res2) >= 1, "应命中第二个事件块"

        # 其他角色不可见
        res_other = store.search("lilith", "眼神让我在意", kind="memory")
        assert res_other == [], "他人不可见的 memory 不应返回"

    @pytest.mark.asyncio
    async def test_index_memory_date_only_targets_date(self, store, tmp_path, monkeypatch):
        """只索引指定日期，其它日期不应被写入。"""
        mem_dir = tmp_path / "mitsuki"
        mem_dir.mkdir(parents=True)
        mem_path = mem_dir / "memory.md"
        mem_path.write_text(
            """
# mitsuki 的长期记忆

## 4月3日
- **时间**：4月3日 上午 08:18 - 08:52
- **地点**：教室
- **在场**：桥本美月、玩家（小明）、莉莉丝（转学生）
- **内容**：今天的事让我在意。

## 4月4日
- **时间**：4月4日 上午 09:00 - 09:30
- **地点**：操场
- **在场**：桥本美月
- **内容**：我独自待了一会儿。
""".strip(),
            encoding="utf-8",
        )

        def mock_character_path(name, subpath=None):
            base = tmp_path / name
            if subpath:
                return str(base / subpath)
            return str(base)

        monkeypatch.setattr(store, "character_path", mock_character_path)

        await store.index_memory_date("mitsuki", "4月3日")

        res_hit = store.search("mitsuki", "让我在意", kind="memory")
        res_miss = store.search("mitsuki", "独自待了一会儿", kind="memory")
        assert len(res_hit) >= 1, "应命中索引的日期"
        assert res_miss == [], "未索引日期不应返回"
