"""单独整理某个角色某一天的记忆。用法: uv run python scripts/consolidate_one_date.py mitsuki 5月18日"""

import asyncio
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from shared.config import CONSOLIDATION_MAX_TOKENS, CONSOLIDATION_TEMPERATURE
from llm.llm_parser import OpenAICompatibleClient
from llm.providers import get_consolidation_llm_config
from memory.consolidator import MemoryConsolidator
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

    consolidator = MemoryConsolidator()
    print("第一步：调用 LLM 进行归并整理...")

    system_step1, user_step1 = consolidator._build_consolidation_prompt_step1(
        agent, sections, [date]
    )
    async with OpenAICompatibleClient(
        **get_consolidation_llm_config(temperature=CONSOLIDATION_TEMPERATURE),
        max_tokens=CONSOLIDATION_MAX_TOKENS, timeout=120.0,
    ) as client:
        result_step1 = await client.chat(
            [{"role": "system", "content": system_step1}, {"role": "user", "content": user_step1}],
            enable_thinking=False,
        )
    step1_result = (result_step1.get("content") or "").strip()

    # 从第一步结果中提取整理后的内容
    m = re.search(r"^.*## 归并整理.*?(?:## |$)", step1_result, re.DOTALL)
    if m:
        step1_content = step1_result[m.end():].strip()
    else:
        step1_content = step1_result.strip()

    # 提取日期对应的内容
    result = re.sub(r"^#{1,6}\s*\d{1,2}月\d{1,2}日\s*\n?", "", step1_content).strip()

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
