#!/usr/bin/env python3
"""向量库查询脚本（适配新表结构：每轮一条 chunk）

Usage:
    # 总览
    uv run python scripts/query_vectors.py list

    # 查看内容（可按可见角色或日期过滤）
    uv run python scripts/query_vectors.py show [--limit 20] [--visible lilith] [--date 4月3日] [--order desc]

    # 统计信息
    uv run python scripts/query_vectors.py stats
"""

import argparse
import asyncio
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv

load_dotenv(project_root / ".env")

from memory.vector_store import vector_store


async def cmd_list():
    """显示全库总览：总条数、覆盖日期、最近写入时间等"""
    await vector_store.init_tables()
    db = await vector_store._get_db()
    try:
        row = await (await db.execute("SELECT COUNT(*), COUNT(DISTINCT date) FROM chunks")).fetchone()
        total, days = row if row else (0, 0)
        row = await (
            await db.execute(
                "SELECT MIN(created_at), MAX(created_at) FROM chunks WHERE created_at IS NOT NULL"
            )
        ).fetchone()
        created_min, created_max = row if row else (None, None)

        print(f"总条数: {total}")
        print(f"覆盖日期(游戏内): {days} 天")
        if created_min or created_max:
            print(f"写入时间范围(UTC): {created_min} ~ {created_max}")

        # 按游戏日期分布（Top 10）
        cur = await db.execute(
            "SELECT date, COUNT(*) AS c FROM chunks GROUP BY date ORDER BY c DESC LIMIT 10"
        )
        rows = await cur.fetchall()
        if rows:
            print("\nTop 日期(按条数):")
            for d, c in rows:
                print(f"  {d or -}: {c}")
    finally:
        await db.close()
        vector_store._db = None


async def cmd_show(limit: int, visible: str | None, date: str | None, order: str):
    """查看 chunks 内容，支持按可见角色/日期过滤"""
    await vector_store.init_tables()
    db = await vector_store._get_db()
    try:
        where = []
        params: list = []
        if visible:
            where.append(
                "EXISTS (SELECT 1 FROM json_each(chunks.visible_to) WHERE json_each.value = ?)"
            )
            params.append(visible)
        if date:
            where.append("date = ?")
            params.append(date)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        order_sql = "DESC" if order.lower().startswith("d") else "ASC"

        sql = (
            "SELECT id, round_id, date, created_at, visible_to, content "
            "FROM chunks" + where_sql + f" ORDER BY id {order_sql} LIMIT ?"
        )
        params2 = params + [limit]
        cur = await db.execute(sql, params2)
        rows = await cur.fetchall()

        # 总数
        sql_cnt = "SELECT COUNT(*) FROM chunks" + where_sql
        total = (await (await db.execute(sql_cnt, params)).fetchone())[0]

        print(f"=== chunks（显示 {len(rows)}/{total}）===\n")
        for row_id, rid, d, created_at, vis_json, content in rows:
            vis = vis_json or "[]"
            preview = content[:200].replace("\n", "\\n")
            if len(content) > 200:
                preview += "…"
            print(f"id={row_id}, round_id={rid}, date={d or -}, created_at={created_at or -}")
            print(f"visible_to={vis}")
            print(f"{len(content)} chars: {preview}\n")
    finally:
        await db.close()
        vector_store._db = None


async def cmd_stats():
    """显示整体统计（长度分布、日期分布 Top）"""
    await vector_store.init_tables()
    db = await vector_store._get_db()
    try:
        total_chunks = (await (await db.execute("SELECT COUNT(*) FROM chunks")).fetchone())[0]
        lens = await (
            await db.execute(
                "SELECT SUM(LENGTH(content)), MIN(LENGTH(content)), MAX(LENGTH(content)), AVG(LENGTH(content)) FROM chunks"
            )
        ).fetchone()
        total_len, min_len, max_len, avg_len = lens if lens else (0, 0, 0, 0)

        print(f"总计: {total_chunks} 个 chunks")
        print(f"字符总数: {total_len}, 最短: {min_len}, 最长: {max_len}, 平均: {avg_len:.0f}")

        cur = await db.execute(
            "SELECT date, COUNT(*) AS c FROM chunks GROUP BY date ORDER BY c DESC LIMIT 10"
        )
        rows = await cur.fetchall()
        if rows:
            print("\nTop 日期(按条数):")
            for d, c in rows:
                print(f"  {d or -}: {c}")
    finally:
        await db.close()
        vector_store._db = None


async def main():
    parser = argparse.ArgumentParser(description="查询向量数据库 chunks 信息")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="显示全库总览")
    sub.add_parser("stats", help="显示整体统计")

    show_parser = sub.add_parser("show", help="查看 chunks 内容")
    show_parser.add_argument("--limit", type=int, default=20, help="显示条数（默认 20）")
    show_parser.add_argument("--visible", type=str, default=None, help="按可见角色过滤（如 lilith）")
    show_parser.add_argument("--date", type=str, default=None, help="按游戏日期过滤（如 4月3日）")
    show_parser.add_argument("--order", type=str, default="asc", help="排序：asc|desc（默认 asc）")

    args = parser.parse_args()

    if args.command == "list":
        await cmd_list()
    elif args.command == "show":
        await cmd_show(args.limit, args.visible, args.date, args.order)
    elif args.command == "stats":
        await cmd_stats()


if __name__ == "__main__":
    asyncio.run(main())
