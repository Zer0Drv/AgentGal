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
from typing import List

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
    from memory import vector_store as vs_module
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
os.makedirs(os.path.dirname(test_db_path), exist_ok=True)
vs_module.DB_PATH = test_db_path


@pytest_asyncio.fixture(scope="function")
async def clean_store():
    """提供清理后的 VectorStore，每个测试隔离。"""
    store = vector_store

    # 重置连接状态，避免跨用例共享缓存
    store._db = None
    store._conv_game_date.clear()

    await store.init_tables()

    # 清空表，保证 DB 层面完全隔离
    db = await store._get_db()
    await db.execute("DELETE FROM vec_chunks")
    await db.execute("DELETE FROM chunks")
    await db.commit()

    yield store

    # 关闭数据库连接
    if store._db is not None:
        await store._db.close()
        store._db = None


async def _wait_for_tasks(store, timeout: float = 5.0):
    """等待所有后台任务完成"""
    # 短暂等待让任务有机会启动
    await asyncio.sleep(0.05)
    if not store._background_tasks:
        return
    await asyncio.wait_for(
        asyncio.gather(*store._background_tasks, return_exceptions=True),
        timeout=timeout
    )


class TestVectorStoreBasic:
    """基础增查删测试"""

    @pytest.mark.asyncio
    async def test_add_round_and_search(self, clean_store):
        """测试写入轮次并搜索"""
        store = clean_store

        # 写入两轮：分别对不同角色可见
        store.add_round(
            ["lilith", "narrator"],
            "testcase_1",
            "玩家: 你好\n旁白: **时间**：4月3日 08:00\nlilith: 早上好",
        )
        store.add_round(
            ["mitsuki", "narrator"],
            "testcase_2",
            "玩家: 去篮球场吗\n旁白: **时间**：4月3日 08:10\nmitsuki: 一起吧",
        )
        await _wait_for_tasks(store)

        # 查询验证：只返回各自可见的内容
        res_lilith = store.search("lilith", "早上好")
        res_mitsuki = store.search("mitsuki", "篮球场")

        assert len(res_lilith) >= 1, "lilith 应该能找到自己的记录"
        assert len(res_mitsuki) >= 1, "mitsuki 应该能找到自己的记录"

    @pytest.mark.asyncio
    async def test_visibility_isolation(self, clean_store):
        """测试可见性隔离：角色只能看到对自己可见的记录"""
        store = clean_store

        # 写入两轮，分别对不同角色可见
        store.add_round(
            ["lilith", "narrator"],
            "testcase_1",
            "玩家: 你好\n旁白: **时间**：4月3日 08:00\nlilith: 早上好",
        )
        store.add_round(
            ["mitsuki", "narrator"],
            "testcase_2",
            "玩家: 去篮球场吗\n旁白: **时间**：4月3日 08:10\nmitsuki: 一起吧",
        )
        await _wait_for_tasks(store)

        # lilith 与 mitsuki 分别只能看到自己的记录
        res_lilith = store.search("lilith", "随便问一句")
        res_mitsuki = store.search("mitsuki", "随便问一句")

        assert len(res_lilith) <= 1, "lilith 不止能看到自己的记录"
        assert len(res_mitsuki) <= 1, "mitsuki 不止能看到自己的记录"

    @pytest.mark.asyncio
    async def test_chunk_fields(self, clean_store):
        """验证 chunk 字段完整性：date/created_at/visible_to"""
        store = clean_store

        store.add_round(
            ["lilith", "narrator"],
            "testcase_1",
            "玩家: 你好\n旁白: **时间**：4月3日 08:00\nlilith: 早上好",
        )
        await _wait_for_tasks(store)

        db = await store._get_db()
        cur = await db.execute(
            "SELECT round_id, date, created_at, visible_to FROM chunks ORDER BY id ASC LIMIT 1"
        )
        row = await cur.fetchone()

        assert row is not None, "应该有数据写入"
        rid, date, created_at, vis = row

        assert rid == "testcase_1"
        assert date == "4月3日", f"日期解析错误: {date}"
        assert created_at is not None, "created_at 不应为空"
        # 验证 visible_to 是合法 JSON
        vis_list = json.loads(vis)
        assert isinstance(vis_list, list)
        assert "lilith" in vis_list
        assert "narrator" in vis_list

    @pytest.mark.asyncio
    async def test_delete(self, clean_store):
        """测试删除指定角色的可见记录"""
        store = clean_store

        # 写入数据：只对 lilith 可见
        store.add_round(
            ["lilith", "narrator"],
            "testcase_1",
            "玩家: 你好\n旁白: **时间**：4月3日 08:00\nlilith: 早上好",
        )
        await _wait_for_tasks(store)

        # 确认有数据
        res_before = store.search("lilith", "早上好")
        assert len(res_before) >= 1, "删除前应该有数据"

        # 删除 lilith 的可见记录
        ok = await store.delete("lilith")
        assert ok is True, "删除应该成功"

        # 验证删除后查询为空
        res_after = store.search("lilith", "早上好")
        assert len(res_after) == 0, "删除后应该没有数据"


class TestVectorStoreRebuild:
    """重建功能测试"""

    @pytest.mark.asyncio
    async def test_rebuild_from_jsonl(self, clean_store, tmp_path, monkeypatch):
        """测试从 narrator/raw/*.jsonl 回放索引"""
        store = clean_store

        # 创建模拟的 jsonl 数据目录
        raw_dir = tmp_path / "narrator" / "raw"
        raw_dir.mkdir(parents=True)

        # 创建模拟的 jsonl 文件：两轮对话，分别可见给不同角色
        jsonl_content = """\
{"role": "player", "content": "你好", "visible_to": ["lilith", "narrator"]}
{"role": "narrator", "content": "**时间**：4月3日 08:00\\n早上好", "visible_to": ["lilith", "narrator"]}
{"role": "player", "content": "去篮球场吗", "visible_to": ["mitsuki", "narrator"]}
{"role": "narrator", "content": "**时间**：4月3日 08:10\\n一起吧", "visible_to": ["mitsuki", "narrator"]}
"""
        (raw_dir / "2024-01-01.jsonl").write_text(jsonl_content, encoding="utf-8")

        # mock character_path 返回我们的临时目录，避免依赖真实角色文件
        def mock_character_path(name, subpath=None):
            if name == "narrator":
                base = tmp_path / "narrator"
            else:
                base = tmp_path / name
            if subpath:
                return str(base / subpath)
            return str(base)

        # mock store 实例的 character_path 方法
        monkeypatch.setattr(store, "character_path", mock_character_path)

        # 执行重建：jsonl -> 轮次 -> 向量库
        await store.rebuild("narrator")

        # 验证重建后可以搜索到数据
        res = store.search("lilith", "早上好")
        assert len(res) >= 1, "重建后应该能搜索到 lilith 的记录"


class TestVectorStoreEdgeCases:
    """边界情况测试"""

    @pytest.mark.asyncio
    async def test_empty_search(self, clean_store):
        """测试空查询返回空结果"""
        store = clean_store

        res = store.search("lilith", "")
        assert res == [], "空查询应该返回空列表"

        res = store.search("lilith", "   ")
        assert res == [], "空白查询应该返回空列表"

    @pytest.mark.asyncio
    async def test_search_nonexistent_agent(self, clean_store):
        """测试搜索不存在的角色"""
        store = clean_store

        # 不存在的角色应不会命中任何 chunk
        res = store.search("nonexistent", "查询")
        assert res == [], "不存在的角色应该返回空列表"

    @pytest.mark.asyncio
    async def test_duplicate_round_id(self, clean_store):
        """测试重复 round_id 不会导致重复数据（INSERT OR REPLACE）"""
        store = clean_store

        # 写入相同 round_id 两次：第二次应覆盖
        store.add_round(
            ["lilith", "narrator"],
            "same_id",
            "玩家: 第一次\n旁白: **时间**：4月3日 08:00\nlilith: 你好",
        )
        store.add_round(
            ["lilith", "narrator"],
            "same_id",
            "玩家: 第二次\n旁白: **时间**：4月3日 08:10\nlilith: 重复",
        )
        await _wait_for_tasks(store)

        # 查询数据库确认只写入了一条
        db = await store._get_db()
        cur = await db.execute("SELECT COUNT(*) FROM chunks WHERE round_id = ?", ("same_id",))
        count = (await cur.fetchone())[0]
        assert count == 1, "重复 round_id 应该只保留一条"

    @pytest.mark.asyncio
    async def test_multiple_conv_id_isolation(self, clean_store):
        """测试不同 conversation_id 的数据隔离"""
        store = clean_store

        # 使用不同的 conv_id（通过 round_id 前缀区分）
        store.add_round(["lilith", "narrator"], "conv1_1", "玩家: 你好\n旁白: **时间**：4月3日\nlilith: 早上好")
        store.add_round(["lilith", "narrator"], "conv2_1", "玩家: 再见\n旁白: **时间**：4月3日\nlilith: 晚安")
        await _wait_for_tasks(store)

        # 两个对话的数据都应该能搜索到
        res = store.search("lilith", "你好")
        assert len(res) >= 1

        res = store.search("lilith", "再见")
        assert len(res) >= 1
