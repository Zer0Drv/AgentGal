#!/usr/bin/env python3
"""记忆整理脚本（稳定版）

这个脚本的职责：
1) 找到 data/characters/*/memory.md
2) 调用 memory.consolidator 对每个 agent 做 LLM 整理
3) **显式管理**整理过程里创建的后台任务（尤其是向量重建 rebuild），避免 asyncio.run
   在退出阶段因为"悬挂任务取消不掉"而表现为卡死/只能 Ctrl-C。

Usage:
    uv run python scripts/consolidate_memories.py
    uv run python scripts/consolidate_memories.py --agents lilith narrator
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path


# 将项目根目录加入路径
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 必须先加载环境变量，再导入模块（它们会读取环境变量）
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from memory.consolidator import memory_consolidator


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Consolidate agents' memory.md")
    p.add_argument(
        "--agents",
        nargs="*",
        default=None,
        help="只处理指定角色（默认自动扫描 data/characters/*/memory.md）",
    )
    p.add_argument(
        "--agent-timeout",
        type=int,
        default=int(os.getenv("CONSOLIDATE_AGENT_TIMEOUT_SECONDS", "0")),
        help="单个 agent 的超时秒数；0 表示不限制（默认从环境变量读取）",
    )
    p.add_argument(
        "--wait-rebuild",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "是否等待整理过程中触发的向量重建任务完成（默认等待；"
            "该默认值不受环境变量影响，如需跳过请显式传 --no-wait-rebuild）"
        ),
    )
    p.add_argument(
        "--rebuild-timeout",
        type=int,
        default=int(os.getenv("CONSOLIDATE_REBUILD_TIMEOUT_SECONDS", "1800")),
        help="等待向量重建的最大秒数（默认 1800；0 表示无限等待）",
    )
    p.add_argument(
        "--shutdown-timeout",
        type=int,
        default=int(os.getenv("CONSOLIDATE_SHUTDOWN_TIMEOUT_SECONDS", "20")),
        help="退出时取消/收尾剩余任务的最大秒数（默认 20）",
    )
    p.add_argument(
        "--heartbeat",
        type=int,
        default=int(os.getenv("CONSOLIDATE_HEARTBEAT_SECONDS", "15")),
        help="心跳间隔秒数；0 关闭（默认 15）",
    )
    return p.parse_args()


def get_all_agents() -> list[str]:
    """获取 data/characters 目录下所有角色名称（以 memory.md 是否存在为准）"""
    agents_dir = PROJECT_ROOT / "data" / "characters"
    if not agents_dir.exists():
        return []

    agents: list[str] = []
    for item in agents_dir.iterdir():
        if not item.is_dir():
            continue
        memory_file = item / "memory.md"
        if memory_file.exists():
            agents.append(item.name)
    return sorted(agents)


async def _heartbeat(*, stop: asyncio.Event, interval_seconds: int) -> None:
    if interval_seconds <= 0:
        return
    start = time.time()
    while not stop.is_set():
        elapsed = int(time.time() - start)
        print(f"[记忆整理] 仍在运行... 已用时 {elapsed}s", flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass


async def _wait_tasks(
    tasks: set[asyncio.Task],
    *,
    label: str,
    timeout_seconds: int,
) -> None:
    tasks = {t for t in tasks if not t.done()}
    if not tasks:
        return

    print(f"[记忆整理] 等待 {label}: {len(tasks)} 个任务", flush=True)
    if timeout_seconds == 0:
        await asyncio.gather(*tasks, return_exceptions=True)
        return

    done, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
    if pending:
        print(
            f"[记忆整理] 等待 {label} 超时，取消剩余 {len(pending)} 个任务",
            flush=True,
        )
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def _cancel_remaining_tasks(*, timeout_seconds: int) -> None:
    """确保事件循环退出前没有悬挂任务，避免 asyncio.run 在关机阶段卡住。"""
    current = asyncio.current_task()
    pending = {
        t
        for t in asyncio.all_tasks()
        if t is not current and not t.done()
    }
    if not pending:
        return

    print(f"[记忆整理] 退出收尾：取消剩余任务 {len(pending)} 个", flush=True)
    for t in pending:
        t.cancel()

    if timeout_seconds <= 0:
        await asyncio.gather(*pending, return_exceptions=True)
        return

    done, still_pending = await asyncio.wait(pending, timeout=timeout_seconds)
    if still_pending:
        # 最后一次 gather，避免 Task 被泄漏
        await asyncio.gather(*still_pending, return_exceptions=True)


async def _close_resources() -> None:
    """尽量关干净：即便 Ctrl-C 触发取消，也不要让 close 半途而废。"""

    async def _close_one(label: str, coro) -> None:
        try:
            await asyncio.shield(coro)
            print(f"[记忆整理] 已关闭: {label}", flush=True)
        except Exception as e:
            print(f"[记忆整理] 关闭 {label} 失败: {e}", flush=True)

    await _close_one("memory_consolidator", memory_consolidator.close())


async def main() -> int:
    args = _parse_args()

    agents = args.agents if args.agents else get_all_agents()
    if not agents:
        print("[记忆整理] 未找到任何角色（data/characters/*/memory.md）", flush=True)
        return 1

    print(f"[记忆整理] 角色: {agents}", flush=True)
    print(
        "[记忆整理] 说明：整理过程中会触发向量重建 rebuild；本脚本会显式等待/取消这些后台任务，"
        "避免退出阶段卡住。",
        flush=True,
    )

    stop = asyncio.Event()
    hb_task = asyncio.create_task(_heartbeat(stop=stop, interval_seconds=args.heartbeat))

    rebuild_tasks: set[asyncio.Task] = set()
    t0 = time.time()

    try:
        for i, agent in enumerate(agents, start=1):
            print(f"[记忆整理] ({i}/{len(agents)}) 开始: {agent}", flush=True)

            # 关键：捕获 consolidate_agent 期间新创建的后台任务（主要是 rebuild）
            before = {
                t
                for t in asyncio.all_tasks()
                if t is not asyncio.current_task()
            }

            coro = memory_consolidator.consolidate_agent(agent)
            if args.agent_timeout and args.agent_timeout > 0:
                await asyncio.wait_for(coro, timeout=args.agent_timeout)
            else:
                await coro

            after = {
                t
                for t in asyncio.all_tasks()
                if t is not asyncio.current_task()
            }
            new_tasks = {t for t in (after - before) if not t.done()}

            # 过滤一下：只保留看起来像"向量重建/同步"的任务，避免误把其他任务也纳入等待
            for t in new_tasks:
                name = getattr(t.get_coro(), "__qualname__", repr(t.get_coro()))
                if "VectorStore" in name or "rebuild" in name or "_sync_incremental" in name:
                    rebuild_tasks.add(t)

            print(
                f"[记忆整理] ({i}/{len(agents)}) 完成: {agent}"
                + (f"（捕获到 {len(rebuild_tasks)} 个向量任务）" if rebuild_tasks else ""),
                flush=True,
            )

        # 可选：等待向量重建任务（否则它们会留到 asyncio.run 关机阶段被取消，容易表现为卡住）
        if args.wait_rebuild:
            await _wait_tasks(
                rebuild_tasks,
                label="向量重建/同步",
                timeout_seconds=args.rebuild_timeout,
            )
        else:
            print(
                f"[记忆整理] 跳过等待向量任务（当前捕获 {len(rebuild_tasks)} 个）",
                flush=True,
            )

        elapsed = time.time() - t0
        print(f"[记忆整理] 全部完成，用时 {elapsed:.1f}s", flush=True)
        return 0

    finally:
        stop.set()
        hb_task.cancel()
        await asyncio.gather(hb_task, return_exceptions=True)

        # 再做一次兜底：取消剩余任务，防止 asyncio.run 在退出阶段卡住
        await _cancel_remaining_tasks(timeout_seconds=args.shutdown_timeout)
        await _close_resources()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("[记忆整理] 已被用户中断 (Ctrl-C)", flush=True)
