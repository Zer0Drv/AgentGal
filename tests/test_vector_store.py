"""
向量库 pytest 测试

测试 sqlite-vec 向量库的增/查/删/重建功能
使用真实 embedding API 请求（不 mock）
"""

import asyncio
import json
import os
import sys
from pathlib import Path

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

# 现在导入 vector_store，此时环境变量已加载
try:
    import importlib
    import memory.vector_store  # 确保子模块被加载进 sys.modules
    vector_store_module = importlib.import_module("memory.vector_store")
    from memory.vector_store import vector_store, EMBED_DIM, EMBED_API_URL, EMBED_API_KEY
except ModuleNotFoundError as exc:
    pytest.skip(f"skip vector_store tests: missing dependency ({exc})", allow_module_level=True)


# 检查 embedding 配置是否可用
pytestmark = pytest.mark.skipif(
    not EMBED_API_URL or not EMBED_API_KEY,
    reason="EMBEDDING_API_URL 或 EMBEDDING_API_KEY 未配置，跳过测试"
)


# 使用测试数据库路径，避免污染真实数据
test_db_path = str(project_root / "data" / "test_vectors.sqlite")


@pytest_asyncio.fixture
async def clean_store(monkeypatch):
    """提供清理后的 VectorStore，每个测试隔离。"""
    # 使用 monkeypatch 修改 DB_PATH，避免全局污染
    monkeypatch.setattr(vector_store_module, "DB_PATH", test_db_path)

    store = vector_store

    # 重置连接状态，避免跨用例共享缓存
    if store._db is not None:
        await store._db.close()
    store._db = None
    store._conv_game_date.clear()

    # 确保目录存在
    os.makedirs(os.path.dirname(test_db_path), exist_ok=True)

    # 删除旧测试数据库
    if os.path.exists(test_db_path):
        os.remove(test_db_path)

    await store.init_tables()

    yield store

    # 清理：关闭数据库连接
    if store._db is not None:
        await store._db.close()
        store._db = None

    # 删除测试数据库文件
    if os.path.exists(test_db_path):
        os.remove(test_db_path)


async def wait_for_search(store, agent_name: str, query: str, kind: str = "memory", timeout: float = 10.0):
    """轮询等待向量检索可命中（超时抛出异常）。"""
    deadline = asyncio.get_event_loop().time() + timeout
    last_error = None

    while asyncio.get_event_loop().time() < deadline:
        try:
            res = store.search(agent_name, query, kind=kind)
            if res:
                return res
        except Exception as e:
            last_error = e
        await asyncio.sleep(0.1)

    error_msg = f"等待搜索结果超时: agent={agent_name}, query={query}, kind={kind}"
    if last_error:
        error_msg += f", last_error={last_error}"
    raise TimeoutError(error_msg)


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


class TestVectorStoreBasic:
    """基础增查删测试"""

    @pytest.mark.asyncio
    async def test_delete(self, clean_store, tmp_path, monkeypatch):
        """测试删除指定角色的可见记录"""
        store = clean_store
        monkeypatch.setattr(store, "character_path", make_character_path(tmp_path))

        write_memory(
            tmp_path,
            "lilith",
            (
                "# lilith 的长期记忆\n\n"
                "## 4月3日\n"
                "- **时间**：4月3日 08:00\n"
                "- **地点**：教室\n"
                "- **在场**：莉莉丝\n"
                "- **内容**：早上好，今天天气不错。"
            ),
        )
        await store.add_memory("lilith", "4月3日")

        res_before = await wait_for_search(store, "lilith", "早上好", kind="memory")
        assert len(res_before) >= 1, "删除前应该有数据"

        ok = await store.delete("lilith")
        assert ok is True, "删除应该成功"

        res_after = store.search("lilith", "早上好", kind="memory")
        assert len(res_after) == 0, "删除后应该没有数据"



class TestVectorStoreRebuild:
    """重建功能测试"""

    @pytest.mark.asyncio
    async def test_rebuild_memory_layer(self, clean_store, tmp_path, monkeypatch):
        """测试 rebuild() 从 memory.md + consolidation_state 重建 memory 层向量索引"""
        import importlib; vs_mod = importlib.import_module("memory.vector_store")

        store = clean_store
        monkeypatch.setattr(store, "character_path", make_character_path(tmp_path))

        write_memory(
            tmp_path,
            "lilith",
            (
                "# lilith 的长期记忆\n\n"
                "## 4月3日\n"
                "- **时间**：4月3日 08:00\n"
                "- **地点**：教室\n"
                "- **在场**：莉莉丝\n"
                "- **内容**：这是可被 rebuild 的记忆内容。"
            ),
        )

        # monkeypatch get_agent_names 和 load_consolidation_state（patch vs_mod 中已导入的引用）
        monkeypatch.setattr(vs_mod, "get_agent_names", lambda: ["lilith"])
        monkeypatch.setattr(vs_mod, "load_consolidation_state", lambda agent: "4月4日")

        await store.rebuild("narrator")

        res = await wait_for_search(store, "lilith", "被 rebuild 的记忆", kind="memory")
        assert len(res) >= 1, "rebuild 后应该能搜索到 lilith 的记忆"



class TestVectorStoreEdgeCases:
    """边界情况测试"""

    @pytest.mark.asyncio
    async def test_empty_search(self, clean_store):
        """测试空查询返回空结果"""
        store = clean_store

        res = store.search("lilith", "", kind="memory")
        assert res == [], "空查询应该返回空列表"

        res = store.search("lilith", "   ", kind="memory")
        assert res == [], "空白查询应该返回空列表"

    @pytest.mark.asyncio
    async def test_search_nonexistent_agent(self, clean_store):
        """测试搜索不存在的角色"""
        store = clean_store

        # 不存在的角色应不会命中任何 chunk
        res = store.search("nonexistent", "查询", kind="memory")
        assert res == [], "不存在的角色应该返回空列表"

    @pytest.mark.asyncio
    async def test_memory_search_isolation(self, clean_store, tmp_path, monkeypatch):
        """测试 memory 层检索：只检索 memory 种类，不同角色互相隔离。"""
        store = clean_store

        write_memory(
            tmp_path,
            "lilith",
            (
                "# lilith 的长期记忆\n\n"
                "## 4月2日\n"
                "- **时间**：4月2日 晚上\n"
                "- **地点**：天台\n"
                "- **在场**：莉莉丝、玩家\n"
                "- **内容**：这是昨天的长期记忆锚点。\n\n"
                "## 4月3日\n"
                "- **时间**：4月3日 上午\n"
                "- **地点**：教室\n"
                "- **在场**：莉莉丝、玩家\n"
                "- **内容**：这是今天的长期记忆，不应进 memory 层。"
            ),
        )

        monkeypatch.setattr(store, "character_path", make_character_path(tmp_path))

        await store.add_memory("lilith", "4月2日")

        res_old = await wait_for_search(store, "lilith", "昨天的长期记忆锚点", kind="memory")
        assert any("昨天的长期记忆锚点" in r["content"] for r in res_old), "应该能搜索到昨天的记忆"

        res_today_mem = store.search("lilith", "今天的长期记忆", kind="memory")
        assert not any("今天的长期记忆" in r["content"] for r in res_today_mem), "4月3日不应在 memory 层"

        res_other = store.search("mitsuki", "昨天的长期记忆锚点", kind="memory")
        assert res_other == [], "mitsuki 不应能看到 lilith 的记忆"

    @pytest.mark.asyncio
    async def test_delete_all_agents_partial(self, clean_store, tmp_path, monkeypatch):
        """测试 delete_all_agents 的逐角色删除路径。"""
        store = clean_store
        monkeypatch.setattr(store, "character_path", make_character_path(tmp_path))

        write_memory(
            tmp_path, "lilith",
            "# lilith\n\n## 4月3日\n- **时间**：4月3日 10:00\n- **地点**：走廊\n- **在场**：莉莉丝\n- **内容**：仅 lilith 可见的记忆。",
        )
        write_memory(
            tmp_path, "mitsuki",
            "# mitsuki\n\n## 4月3日\n- **时间**：4月3日 10:01\n- **地点**：操场\n- **在场**：美月\n- **内容**：仅 mitsuki 可见的记忆。",
        )

        await store.add_memory("lilith", "4月3日")
        await store.add_memory("mitsuki", "4月3日")

        await wait_for_search(store, "lilith", "仅 lilith 可见的记忆", kind="memory")
        await wait_for_search(store, "mitsuki", "仅 mitsuki 可见的记忆", kind="memory")

        result = await store.delete_all_agents(["lilith"])
        assert result == {"lilith": True}

        res_lilith = store.search("lilith", "仅 lilith 可见的记忆", kind="memory")
        res_mitsuki = store.search("mitsuki", "仅 mitsuki 可见的记忆", kind="memory")
        assert len(res_lilith) == 0, "lilith 的数据应该被删除"
        assert len(res_mitsuki) >= 1, "mitsuki 的数据应该保留"

    @pytest.mark.asyncio
    async def test_delete_all_agents_full_clear(self, clean_store, tmp_path, monkeypatch):
        """测试 delete_all_agents 命中全角色时走全量清理。"""
        import importlib; vs_mod = importlib.import_module("memory.vector_store")

        store = clean_store
        monkeypatch.setattr(store, "character_path", make_character_path(tmp_path))

        write_memory(
            tmp_path, "lilith",
            "# lilith\n\n## 4月2日\n- **时间**：4月2日 晚上\n- **地点**：天台\n- **在场**：莉莉丝、玩家\n- **内容**：这是昨天的长期记忆锚点。",
        )
        await store.add_memory("lilith", "4月2日")
        await wait_for_search(store, "lilith", "昨天的长期记忆锚点", kind="memory")

        monkeypatch.setattr(vs_mod, "get_agent_names", lambda: ["lilith", "mitsuki", "narrator"])
        result = await store.delete_all_agents(["lilith", "mitsuki", "narrator"])
        assert result == {"lilith": True, "mitsuki": True, "narrator": True}

        db = await store._get_db()
        chunk_count = (await (await db.execute("SELECT COUNT(*) FROM chunks")).fetchone())[0]
        vec_count = (await (await db.execute("SELECT COUNT(*) FROM vec_chunks")).fetchone())[0]
        assert chunk_count == 0, f"chunks 表应该为空，实际有{chunk_count}条"
        assert vec_count == 0, f"vec_chunks 表应该为空，实际有{vec_count}条"


class TestVectorStoreMemoryIndexing:
    """长期记忆索引测试"""

    @pytest.mark.asyncio
    async def test_add_memory_splits_events(self, clean_store, tmp_path, monkeypatch):
        """索引指定日期的 memory.md，按事件块拆分并可检索。"""
        store = clean_store

        write_memory(
            tmp_path,
            "mitsuki",
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
        )

        monkeypatch.setattr(store, "character_path", make_character_path(tmp_path))
        await store.add_memory("mitsuki", "4月3日")

        # 等待索引完成并验证结果
        res1 = await wait_for_search(store, "mitsuki", "眼神让我在意", kind="memory")
        res2 = await wait_for_search(store, "mitsuki", "下午的安排", kind="memory")
        assert len(res1) >= 1, "应命中第一个事件块"
        assert len(res2) >= 1, "应命中第二个事件块"

        res_other = store.search("lilith", "眼神让我在意", kind="memory")
        assert res_other == [], "他人不可见的 memory 不应返回"

    @pytest.mark.asyncio
    async def test_add_memory_only_targets_date(self, clean_store, tmp_path, monkeypatch):
        """只索引指定日期，其它日期不应被写入。"""
        store = clean_store

        write_memory(
            tmp_path,
            "mitsuki",
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
        )

        monkeypatch.setattr(store, "character_path", make_character_path(tmp_path))
        await store.add_memory("mitsuki", "4月3日")

        # 等待索引完成并验证结果
        res_hit = await wait_for_search(store, "mitsuki", "让我在意", kind="memory")
        res_miss = store.search("mitsuki", "独自待了一会儿", kind="memory")
        assert len(res_hit) >= 1, "应命中索引的日期"
        assert not any("独自待了一会儿" in r["content"] for r in res_miss), "未索引日期不应返回"
