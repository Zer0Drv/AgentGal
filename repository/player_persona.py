"""玩家「我」的人设卡（顶层 sidecar，与 .player_name 并列）。

人设卡采用与角色 soul.md 一致的六段结构：
<identity> / <goal> / <past> / <habits> / <reactions> / <voice>
由用户在前端「我的人设」编辑，注入旁白与角色 prompt，让系统理解玩家是谁。
"""

import re
from pathlib import Path

from repository.config import CHARACTERS_DIR

PLAYER_PERSONA_FILENAME = ".player_persona.md"

DEFAULT_PLAYER_PERSONA = """<identity>
玩家自己会在这里补一句话自我介绍。
</identity>

<goal>
想在后端达成什么、或想和哪个角色走近——玩家自己写。
</goal>

<past>
过往经历、来到这个世界的缘由——玩家自己写。
</past>

<habits>
生活习惯、行事的惯性——玩家自己写。
</habits>

<reactions>
遇到某些情形时的本能反应——玩家自己写。
</reactions>

<voice>
说话的风格、常用的语气词——玩家自己写。
</voice>
"""

_SECTION_TAGS = ["identity", "goal", "past", "habits", "reactions", "voice"]


def persona_path() -> Path:
    return CHARACTERS_DIR / PLAYER_PERSONA_FILENAME


def read_player_persona() -> str:
    """读取玩家 persona；不存在返回空串（不注入模板文案）。"""
    p = persona_path()
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8").strip()


def write_player_persona(text: str) -> None:
    """写入玩家 persona；空文本不写。"""
    if not (t := text.strip()):
        return
    CHARACTERS_DIR.mkdir(parents=True, exist_ok=True)
    persona_path().write_text(t, encoding="utf-8")


def clear() -> None:
    """删除玩家 persona（新开局/清空用）。"""
    persona_path().unlink(missing_ok=True)


def parse_sections(text: str) -> dict[str, str]:
    """把六段 markdown 解析成 {tag: content}；未出现的段返回空串。"""
    sections = {tag: "" for tag in _SECTION_TAGS}
    for tag in _SECTION_TAGS:
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text or "", re.S)
        if m:
            sections[tag] = m.group(1).strip()
    return sections


def build_player_block(display_name: str, persona: str) -> str:
    """构造注入 prompt 的 <player> 块：显示名 + 六段人设。"""
    if not display_name and not (persona or "").strip():
        return ""
    lines = ["<player>"]
    if display_name:
        lines.append(f"display_name: {display_name}")
    if (persona or "").strip():
        lines.append("persona:")
        lines.append(persona.strip())
    lines.append("</player>")
    return "\n".join(lines)
