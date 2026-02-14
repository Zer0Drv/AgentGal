#!/usr/bin/env python3
"""向量库查询脚本 - 查看 chunks 信息

Usage:
    uv run python scripts/query_vectors.py list
    uv run python scripts/query_vectors.py show <agent_name> [--limit 10]
    uv run python scripts/query_vectors.py stats
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv(project_root / ".env")

from core.vector_store import vector_store


async def cmd_list():
    """列出所有 agent 及其 chunk 数量和同步状态"""
    await vector_store.init_tables()
    db = await vector_store._get_db()
    try:
        # 每个 agent 的 chunk 数量
        cursor = await db.execute(
            "SELECT agent_name, COUNT(*) FROM chunks GROUP BY agent_name ORDER BY agent_name"
        )
        chunk_counts = {row[0]: row[1] for row in await cursor.fetchall()}

        # 同步状态
        cursor = await db.execute("SELECT agent_name, sync_offset, content_hash FROM sync_state")
        sync_rows = {row[0]: (row[1], row[2]) for row in await cursor.fetchall()}

        all_agents = sorted(set(list(chunk_counts.keys()) + list(sync_rows.keys())))

        if not all_agents:
            print("向量库为空，没有任何 agent 数据。")
            return

        print(f"{'Agent':<20} {'Chunks':>8} {'Sync Offset':>13} {'Content Hash':>16}")
        print("-" * 60)
        for name in all_agents:
            count = chunk_counts.get(name, 0)
            offset, hash_val = sync_rows.get(name, (0, ""))
            short_hash = hash_val[:12] + "…" if hash_val and len(hash_val) > 12 else hash_val or "-"
            print(f"{name:<20} {count:>8} {offset:>13} {short_hash:>16}")
    finally:
        await db.close()


async def cmd_show(agent_name: str, limit: int):
    """查看某个 agent 的 chunks 内容"""
    await vector_store.init_tables()
    db = await vector_store._get_db()
    try:
        cursor = await db.execute(
            "SELECT id, chunk_index, content FROM chunks "
            "WHERE agent_name = ? ORDER BY chunk_index ASC LIMIT ?",
            (agent_name, limit),
        )
        rows = await cursor.fetchall()

        if not rows:
            print(f"未找到 {agent_name} 的 chunks。")
            return

        # 总数
        cursor = await db.execute(
            "SELECT COUNT(*) FROM chunks WHERE agent_name = ?", (agent_name,)
        )
        total = (await cursor.fetchone())[0]

        print(f"=== {agent_name} 的 chunks（显示 {len(rows)}/{total}）===\n")
        for row_id, chunk_idx, content in rows:
            preview = content[:200].replace("\n", "\\n")
            if len(content) > 200:
                preview += "…"
            print(f"[{chunk_idx}] (id={row_id}, {len(content)} 字符)")
            print(f"    {preview}")
            print()
    finally:
        await db.close()


async def cmd_stats():
    """显示整体统计"""
    await vector_store.init_tables()
    db = await vector_store._get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM chunks")
        total_chunks = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COUNT(DISTINCT agent_name) FROM chunks")
        total_agents = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT agent_name, COUNT(*), SUM(LENGTH(content)), "
            "MIN(LENGTH(content)), MAX(LENGTH(content)), AVG(LENGTH(content)) "
            "FROM chunks GROUP BY agent_name ORDER BY agent_name"
        )
        rows = await cursor.fetchall()

        print(f"总计: {total_agents} 个 agent, {total_chunks} 个 chunks\n")

        if rows:
            print(f"{'Agent':<20} {'Chunks':>8} {'总字符':>10} {'最短':>8} {'最长':>8} {'平均':>8}")
            print("-" * 65)
            for name, count, total_len, min_len, max_len, avg_len in rows:
                print(f"{name:<20} {count:>8} {total_len:>10} {min_len:>8} {max_len:>8} {avg_len:>8.0f}")
    finally:
        await db.close()


async def main():
    parser = argparse.ArgumentParser(description="查询向量数据库 chunks 信息")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出所有 agent 及 chunk 数量")
    sub.add_parser("stats", help="显示整体统计")

    show_parser = sub.add_parser("show", help="查看某个 agent 的 chunks")
    show_parser.add_argument("agent_name", help="角色名称")
    show_parser.add_argument("--limit", type=int, default=20, help="显示条数（默认 20）")

    args = parser.parse_args()

    try:
        if args.command == "list":
            await cmd_list()
        elif args.command == "show":
            await cmd_show(args.agent_name, args.limit)
        elif args.command == "stats":
            await cmd_stats()
    finally:
        await vector_store.close()


if __name__ == "__main__":
    asyncio.run(main())

