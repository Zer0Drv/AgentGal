#!/usr/bin/env python3
"""
Evaluate RAG retrieval quality for the last N character agent calls
by submitting a batch judge prompt to Codex (codex exec).

Usage:
    python scripts/analyze_rag.py          # default: last 50 char calls
    python scripts/analyze_rag.py --n 20
    python scripts/analyze_rag.py --dry-run   # print prompt without calling codex
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

LOG_PATH = Path(__file__).parent.parent / "logs" / "agent" / "agent_calls.jsonl"
SCHEMA_PATH = Path(__file__).parent.parent / "scripts" / "_rag_judge_schema.json"

JUDGE_SCHEMA = {
    "type": "object",
    "required": ["scores"],
    "additionalProperties": False,
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "score", "noise_count", "reason"],
                "additionalProperties": False,
                "properties": {
                    "id":          {"type": "integer"},
                    "score":       {"type": "integer"},
                    "noise_count": {"type": "integer"},
                    "reason":      {"type": "string"},
                },
            },
        },
    },
}

JUDGE_SYSTEM_PROMPT = """\
你是一个角色扮演游戏的RAG质量评估员。

游戏背景：多角色恋爱叙事游戏，角色有长期记忆，系统会在每轮对话前检索角色的相关历史记忆，
注入到角色的上下文中，帮助角色表现出连贯的人格和记忆。

你的任务：判断"检索到的记忆"是否与"当前场景"自然相关——即角色在此刻是否会真实地想起这些记忆。

评分标准：
5 - 所有或大多数记忆与当前场景高度吻合，角色自然会浮现这些
4 - 大部分相关，个别稍显牵强
3 - 一半相关一半无关
2 - 大部分是噪声，仅少数相关
1 - 全部噪声，与当前场景完全无关

请注意：这是叙事游戏，相关性是"情绪/情境上的自然联想"，不是字面关键词匹配。
例如：当前场景是两人安静待在一起，检索到一条关于第一次牵手的记忆，即使没有关键词重合也是高度相关的。

请严格按照JSON schema输出，不要输出其他内容。
"""


def load_entries(path: Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def extract_relevant_memories(user_content: str) -> str:
    m = re.search(r"<relevant_memories>(.*?)</relevant_memories>", user_content, re.DOTALL)
    return m.group(1).strip() if m else ""


def extract_current_input(user_content: str) -> str:
    m = re.search(r"</relevant_memories>\s*[-\s]*\n+玩家新消息:\s*(.+?)$", user_content, re.DOTALL)
    if m:
        return m.group(1).strip()
    # fallback: last non-empty line
    lines = [l.strip() for l in user_content.splitlines() if l.strip()]
    return lines[-1] if lines else ""


def extract_recent_turns(user_content: str, n_turns: int = 4) -> str:
    """Extract last n_turns of dialogue before <relevant_memories>."""
    # cut at relevant_memories
    cut_idx = user_content.find("<relevant_memories>")
    text = user_content[:cut_idx] if cut_idx >= 0 else user_content

    # also cut before metadata blocks that follow the conversation
    for tag in ("<user_profile>", "<status>", "<relevant_memories>"):
        tag_idx = text.find(tag)
        if tag_idx >= 0:
            text = text[:tag_idx]

    # find lines that look like dialogue turns
    turn_pattern = re.compile(r"^(玩家|narrator|chenxiao|guyining|lilith|mitsuki)[:：]", re.MULTILINE)
    matches = list(turn_pattern.finditer(text))
    if not matches:
        return ""

    last_matches = matches[-n_turns:]
    start = last_matches[0].start()
    return text[start:].strip()


def build_judge_prompt(cases: list[dict]) -> str:
    lines = [JUDGE_SYSTEM_PROMPT, "\n以下是需要评估的案例，请对每个案例输出一条JSON评分：\n"]

    for c in cases:
        lines.append(f"=== Case {c['id']} | agent={c['agent']} | ts={c['timestamp'][:16]} ===")
        lines.append("\n【近期对话（最近4轮）】")
        lines.append(c["recent_turns"] or "(无)")
        lines.append("\n【当前玩家输入】")
        lines.append(c["current_input"] or "(无)")
        lines.append("\n【检索到的记忆】")
        lines.append(c["memories"] or "(无记忆)")
        lines.append("")

    lines.append('\n请输出一个JSON对象，格式为 {"scores": [...]}，数组中每个元素对应一个Case，按id顺序排列。')
    return "\n".join(lines)


def call_codex(prompt: str, schema_path: Path) -> list[dict]:
    """Call codex exec, return parsed JSON array of scores."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as out_f:
        out_path = out_f.name

    try:
        result = subprocess.run(
            ["codex", "exec",
             "--output-schema", str(schema_path),
             "-o", out_path,
             "--ephemeral",
             "-s", "read-only",
             "-"],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

        if result.returncode != 0:
            print("STDERR:", result.stderr[:2000], file=sys.stderr)
            raise RuntimeError(f"codex exec failed with code {result.returncode}")

        output = Path(out_path).read_text(encoding="utf-8")
    finally:
        Path(out_path).unlink(missing_ok=True)

    # parse JSON — top-level is {"scores": [...]}
    try:
        data = json.loads(output)
        if isinstance(data, dict) and "scores" in data:
            return data["scores"]
        if isinstance(data, list):
            return data
        raise ValueError(f"Unexpected structure: {type(data)}")
    except (json.JSONDecodeError, ValueError):
        m = re.search(r"\[.*\]", output, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        print(f"Failed to parse codex output:\n{output[:1000]}", file=sys.stderr)
        raise


def print_results(scores: list[dict], cases: list[dict]) -> None:
    case_map = {c["id"]: c for c in cases}

    print()
    print("=" * 70)
    print("RAG RETRIEVAL QUALITY REPORT")
    print("=" * 70)

    score_counts = [0] * 6  # index 1-5
    total_noise = 0

    print(f"\n{'ID':>4} {'Agent':<12} {'Score':>6} {'Noise':>6}  Reason")
    print("-" * 70)
    for s in sorted(scores, key=lambda x: x["id"]):
        score = s["score"]
        noise = s["noise_count"]
        score_counts[score] += 1
        total_noise += noise
        bar = "★" * score + "☆" * (5 - score)
        print(f"{s['id']:>4} {case_map[s['id']]['agent']:<12} {bar}  noise={noise}  {s['reason'][:55]}")

    n = len(scores)
    avg_score = sum(s["score"] for s in scores) / n
    avg_noise = total_noise / n

    print()
    print("=" * 50)
    print("SUMMARY")
    print("=" * 50)
    print(f"Cases evaluated : {n}")
    print(f"Avg score       : {avg_score:.2f} / 5")
    print(f"Avg noise count : {avg_noise:.2f} per call")
    print()
    print("Score distribution:")
    for i in range(1, 6):
        bar = "█" * score_counts[i]
        pct = score_counts[i] / n * 100 if n else 0
        print(f"  {i}★  {bar:<20} {score_counts[i]:>3} ({pct:.0f}%)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate RAG quality via Codex judge")
    parser.add_argument("--n", type=int, default=50, help="Last N character calls to evaluate")
    parser.add_argument("--dry-run", action="store_true", help="Print prompt only, don't call codex")
    args = parser.parse_args()

    entries = load_entries(LOG_PATH)
    char_entries = [e for e in entries if e["agent"] not in ("choices", "narrator")]
    last_n = char_entries[-args.n:]

    # only entries that actually have retrieved memories
    cases = []
    for i, e in enumerate(last_n):
        user_content = next(
            (m["content"] for m in e.get("request", []) if m["role"] == "user"), ""
        )
        memories = extract_relevant_memories(user_content)
        if not memories:
            continue
        cases.append({
            "id": i + 1,
            "agent": e["agent"],
            "timestamp": e.get("timestamp", ""),
            "recent_turns": extract_recent_turns(user_content),
            "current_input": extract_current_input(user_content),
            "memories": memories,
        })

    if not cases:
        print("No entries with relevant_memories found.")
        sys.exit(0)

    print(f"Evaluating {len(cases)} cases (from last {args.n} character calls)...")

    if args.dry_run:
        prompt = build_judge_prompt(cases[:3])
        print("\n--- JUDGE PROMPT (dry run, first 3 cases) ---\n")
        print(prompt[:3000])
        if len(prompt) > 3000:
            print(f"\n... ({len(prompt)} chars total)")
        return

    # write schema file
    SCHEMA_PATH.parent.mkdir(exist_ok=True)
    SCHEMA_PATH.write_text(json.dumps(JUDGE_SCHEMA, ensure_ascii=False, indent=2))

    # batch into chunks of 10
    batch_size = 10
    all_scores: list[dict] = []
    try:
        for i in range(0, len(cases), batch_size):
            batch = cases[i:i + batch_size]
            print(f"  Batch {i // batch_size + 1}: cases {batch[0]['id']}–{batch[-1]['id']} ...")
            prompt = build_judge_prompt(batch)
            scores = call_codex(prompt, SCHEMA_PATH)
            all_scores.extend(scores)
    finally:
        SCHEMA_PATH.unlink(missing_ok=True)

    print_results(all_scores, cases)


if __name__ == "__main__":
    main()
