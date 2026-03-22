#!/usr/bin/env python3
"""Analyze prompt cache hit statistics from agent_calls.jsonl."""

import json
import sys
from collections import defaultdict
from pathlib import Path

LOG_PATH = Path(__file__).parent.parent / "logs" / "agent" / "agent_calls.jsonl"


def load_entries(path: Path) -> list[dict]:
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def analyze_cache(entries: list[dict]) -> None:
    agent_stats: dict[str, dict] = defaultdict(lambda: {
        "calls": 0,
        "total_input": 0,
        "total_cache_hit": 0,
        "ratios": [],
    })

    for e in entries:
        agent = e["agent"]
        m = e.get("metrics", {})
        input_tok = m.get("input_tokens", 0)
        cache_tok = m.get("cache_hit_tokens", 0)
        ratio_str = m.get("cache_hit_ratio", "0%")
        ratio = float(ratio_str.rstrip("%"))

        s = agent_stats[agent]
        s["calls"] += 1
        s["total_input"] += input_tok
        s["total_cache_hit"] += cache_tok
        s["ratios"].append(ratio)

    # --- per-agent table ---
    print("=" * 72)
    print("CACHE HIT STATS BY AGENT")
    print("=" * 72)
    header = f"{'Agent':<20} {'Calls':>6} {'Avg Input':>10} {'Avg Cache':>10} {'Avg Ratio':>10} {'Total Saved':>12}"
    print(header)
    print("-" * 72)

    total_calls = total_input = total_cache = 0

    for agent, s in sorted(agent_stats.items()):
        n = s["calls"]
        avg_in = s["total_input"] / n
        avg_cache = s["total_cache_hit"] / n
        avg_ratio = sum(s["ratios"]) / n
        total_calls += n
        total_input += s["total_input"]
        total_cache += s["total_cache_hit"]
        print(f"{agent:<20} {n:>6} {avg_in:>10.0f} {avg_cache:>10.0f} {avg_ratio:>9.1f}% {s['total_cache_hit']:>12,}")

    print("-" * 72)
    overall = total_cache / total_input * 100 if total_input else 0
    print(f"{'TOTAL':<20} {total_calls:>6} {total_input/total_calls:>10.0f} "
          f"{total_cache/total_calls:>10.0f} {overall:>9.1f}% {total_cache:>12,}")

    # --- date trend (character agents only) ---
    date_stats: dict[str, dict] = defaultdict(lambda: {"calls": 0, "input": 0, "cache": 0})
    for e in entries:
        if e["agent"] in ("choices", "narrator"):
            continue
        m = e.get("metrics", {})
        date = e.get("timestamp", "")[:10] or "unknown"
        ds = date_stats[date]
        ds["calls"] += 1
        ds["input"] += m.get("input_tokens", 0)
        ds["cache"] += m.get("cache_hit_tokens", 0)

    print()
    print("=" * 50)
    print("CACHE RATIO TREND (character agents only)")
    print("=" * 50)
    print(f"{'Date':<12} {'Calls':>6} {'Cache Ratio':>12}  {'':}")
    print("-" * 50)
    for date in sorted(date_stats):
        ds = date_stats[date]
        ratio = ds["cache"] / ds["input"] * 100 if ds["input"] else 0
        bar = "█" * int(ratio / 4)
        print(f"{date:<12} {ds['calls']:>6} {ratio:>10.1f}%  {bar}")


def main() -> None:
    if not LOG_PATH.exists():
        print(f"Log not found: {LOG_PATH}", file=sys.stderr)
        sys.exit(1)
    entries = load_entries(LOG_PATH)
    print(f"Loaded {len(entries)} entries\n")
    analyze_cache(entries)


if __name__ == "__main__":
    main()
