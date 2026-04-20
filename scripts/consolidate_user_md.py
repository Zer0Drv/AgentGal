"""一次性脚本：整理指定角色的 user.md / tmp_user.md 草稿。"""

import asyncio
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from storage.agent_files import cleanup_old_backups
from shared.config import character_path
from consolidation.flow import MemoryConsolidationFlow


async def main(agent: str, *, auto_write: bool = False) -> None:
    user_path = Path(character_path(agent, "user.md"))
    tmp_path = Path(character_path(agent, "tmp_user.md"))
    draft_path = tmp_path if tmp_path.exists() else user_path
    if not draft_path.exists():
        raise FileNotFoundError(f"未找到档案文件: {draft_path}")

    content = draft_path.read_text(encoding="utf-8")
    print(f"草稿来源: {draft_path}")
    print(f"原始: {len(content)} 字")

    print("调用 LLM 中...")
    consolidator = MemoryConsolidationFlow()
    extracted = await consolidator.run_player_profile_consolidation(agent, content)

    if extracted is None:
        print("❌ 整理失败，请查看日志")
        return

    print(
        f"整理后: {len(extracted)} 字 (压缩 {(1 - len(extracted) / len(content)) * 100:.1f}%)"
    )
    print()
    print(extracted)
    print()

    should_write = auto_write
    if not should_write:
        confirm = input("写入文件? [y/N] ")
        should_write = confirm.strip().lower() == "y"

    if should_write:
        bak_dir = Path(character_path(agent, "bak"))
        bak_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        shutil.copy2(user_path, bak_dir / f"user_{ts}_pre.md")
        # 只保留最近 10 个备份
        cleanup_old_backups(bak_dir, "user_*_pre.md", max_count=10)
        user_path.write_text(extracted + "\n", encoding="utf-8")
        tmp_path.unlink(missing_ok=True)
        print("✅ 已备份并写入")
    else:
        print("已取消")


if __name__ == "__main__":
    args = [arg for arg in sys.argv[1:] if arg]
    auto_write = "--yes" in args
    args = [arg for arg in args if arg != "--yes"]
    agent = args[0] if args else "mitsuki"
    asyncio.run(main(agent, auto_write=auto_write))
