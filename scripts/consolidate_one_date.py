"""单独整理某个角色某一天的记忆。用法: uv run python scripts/consolidate_one_date.py mitsuki 5月18日"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from engine.consolidation_flow import MemoryConsolidationFlow
from memory.parser import normalize, split_by_date


async def main(agent: str, date: str) -> None:
    p = Path(f"data/characters/{agent}/memory.md")
    content = normalize(p.read_text())
    sections = split_by_date(content)

    if date not in sections:
        print(f"❌ {agent} 没有 {date} 的记忆")
        return

    old_text = sections[date]
    print(f"原始: {len(old_text)} 字")

    consolidator = MemoryConsolidationFlow()
    print("调用 LLM 进行归并整理...")

    result, message = await consolidator.run_memory_merge_for_date(agent, date, sections)
    print(message)

    if result is None:
        return

    print(
        f"整理后: {len(result)} 字 (压缩 {(1 - len(result) / len(old_text)) * 100:.1f}%)"
    )
    print()
    print(result)
    print()

    confirm = input("写入文件? [y/N] ")
    if confirm.strip().lower() == "y":
        sections[date] = result
        lines: list[str] = []
        for d, s in sections.items():
            lines.append(f"## {d}")
            lines.append(s)
            lines.append("")
        p.write_text("\n".join(lines).strip() + "\n")
        print("✅ 已写入")
    else:
        print("已取消")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("用法: uv run python scripts/consolidate_one_date.py <agent> <日期>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
