"""记忆系统文件操作 - 底层文件操作函数

注意：当前 Agent 通过 XML 解析响应手动触发更新，
而非通过 Tool 调用。本模块保留底层文件操作供其他模块使用。
"""

import json
import os
import re
import shutil
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from engine.config import character_path

_EMPTY_PLACEHOLDER = "（暂无）"
_GROWTH_TITLE = "# 心路历程"


def extract_game_date(text: str) -> str | None:
    """从文本中提取游戏日期（如 4月3日）"""
    m = re.search(r"\*\*时间\*\*：\s*(\d{1,2}月\d{1,2}日)", text)
    if m:
        return m.group(1)
    return None


def parse_cn_date(date_text: str) -> tuple[int, int] | None:
    """解析中文日期格式（如 4月3日）为元组 (月, 日)"""
    m = re.fullmatch(r"(\d{1,2})月(\d{1,2})日", (date_text or "").strip())
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    if 1 <= month <= 12 and 1 <= day <= 31:
        return month, day
    return None


def is_date_before(date_text: str, cutoff_date: str) -> bool:
    """判断 date_text 是否在 cutoff_date 之前"""
    left = parse_cn_date(date_text)
    right = parse_cn_date(cutoff_date)
    if left is None or right is None:
        return False
    return left < right

# 每个角色 status.md 允许的字段白名单（对应 ## 标题）
STATUS_FIELDS: dict[str, list[str]] = {
    "lilith": ["身份", "心境", "我和他", "在意的事", "打算"],
    "mitsuki": ["心境", "我和他", "在意的事", "打算"],
    "narrator": ["故事阶段", "当前时间", "场景", "叙事焦点", "待触发事件"],
}

# 每个角色 user.md 允许的字段白名单
USER_FIELDS: dict[str, list[str]] = {
    "lilith": ["基本信息", "观察到的特质", "互动模式"],
    "mitsuki": ["基本信息", "观察到的特质", "互动模式"],
    "narrator": ["玩家风格", "关键选择", "当前倾向"],
}


def normalize(content: str) -> str:
    """修复常见格式问题：字面\\n、日期标题不规范"""
    # 1. 字面 \n → 真换行
    content = content.replace("\\n", "\n")
    # 2. 清理 HTML 注释
    content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
    # 3. 统一日期标题为 ## X月X日（只处理独占一行的日期标题）
    lines = content.split("\n")
    out = []
    for line in lines:
        m = re.match(r"^(?:##\s*|\*\*)?(\d{1,2}月\d{1,2}日)(?:\*\*)?\s*$", line.strip())
        if m:
            out.append(f"## {m.group(1)}")
        else:
            out.append(line)
    # 4. 压缩连续空行
    content = "\n".join(out)
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content


def split_by_date(content: str) -> OrderedDict[str, str]:
    """按 ## X月X日 切分，同日期自动合并。

    Returns:
        sections: 日期 → 合并后的内容（保持出现顺序）
    """
    sections: OrderedDict[str, str] = OrderedDict()
    current_date = None
    current_lines: list[str] = []

    for line in content.split("\n"):
        m = re.match(r"^##\s*(\d{1,2}月\d{1,2}日)$", line.strip())
        if m:
            if current_date:
                body = "\n".join(current_lines).strip()
                if current_date in sections:
                    sections[current_date] += "\n" + body
                else:
                    sections[current_date] = body
            current_date = m.group(1)
            current_lines = []
        elif current_date:
            current_lines.append(line)
        # 忽略日期之前的内容（标题行等）

    # 最后一段
    if current_date:
        body = "\n".join(current_lines).strip()
        if current_date in sections:
            sections[current_date] += "\n" + body
        else:
            sections[current_date] = body

    return sections


def split_events_raw(content: str) -> list[tuple[str | None, str]]:
    """按 - **时间**：字段分割内容为事件列表。

    格式：- **时间**：4月3日 上午

    Returns:
        [(日期, 事件内容), ...]，日期从时间字段的日期部分提取
    """
    time_pattern = re.compile(r"^-\s+\*\*时间\*\*：(\d{1,2}月\d{1,2}日)")
    events: list[tuple[str | None, str]] = []
    current_date: str | None = None
    current_lines: list[str] = []

    for line in content.split("\n"):
        m = time_pattern.match(line.strip())
        if m:
            if current_lines:
                events.append((current_date, "\n".join(current_lines).strip()))
            current_date = m.group(1)
            current_lines = [line]
        elif current_date is not None:
            current_lines.append(line)

    if current_lines:
        events.append((current_date, "\n".join(current_lines).strip()))

    return events


def split_into_events(day_content: str) -> list[str]:
    """将单日内容按事件分割为列表。无法识别时整体作为一个事件返回。"""
    events = [
        event_text for _, event_text in split_events_raw(day_content) if event_text
    ]
    return events if events else [day_content.strip()]


def _get_fields_from_file(file_path: str) -> list[str] | None:
    """从文件中提取 ## 标题列表

    Args:
        file_path: 文件路径

    Returns:
        文件中所有 ## 标题列表，文件不存在返回 None
    """
    if not os.path.exists(file_path):
        return None
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    fields = []
    for line in content.split("\n"):
        if line.startswith("## "):
            fields.append(line[3:].strip())
    return fields


def get_allowed_fields(agent_name: str, file_type: str) -> list[str]:
    """获取允许字段列表（文件驱动，失败回退到默认值）

    Args:
        agent_name: 角色名
        file_type: "status" | "user"

    Returns:
        文件中所有 ## 标题列表，文件不存在则返回默认字段
    """
    file_path = character_path(agent_name, f"{file_type}.md")
    fields = _get_fields_from_file(file_path)
    if fields is not None:
        return fields
    # 回退到默认值
    defaults = STATUS_FIELDS if file_type == "status" else USER_FIELDS
    return defaults.get(agent_name, [])


def _parse_section_file(
    file_path: str,
    allowed_sections: list[str],
) -> dict[str, str]:
    """解析 section 式文件，返回 {字段名: 内容} 字典。

    自动确保所有白名单字段存在。
    """
    existing_content = ""
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            existing_content = f.read()

    sections: dict[str, str] = {}
    current_sec = None
    current_lines: list[str] = []

    for line in existing_content.split("\n"):
        if line.startswith("## "):
            if current_sec:
                sections[current_sec] = "\n".join(current_lines).strip()
            current_sec = line[3:].strip()
            current_lines = []
        elif current_sec is not None:
            current_lines.append(line)

    if current_sec:
        sections[current_sec] = "\n".join(current_lines).strip()

    for field in allowed_sections:
        if field not in sections:
            sections[field] = _EMPTY_PLACEHOLDER

    return sections


def _write_section_file(
    file_path: str,
    sections: dict[str, str],
    allowed_sections: list[str],
    title_line: str,
) -> None:
    """将 sections 字典写回文件，按白名单顺序排列。"""
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    lines = [title_line, ""]
    for sec in allowed_sections:
        if sec in sections:
            lines.append(f"## {sec}")
            lines.append(sections[sec])
            lines.append("")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _prepare_section_update(
    file_path: str,
    content: str,
    allowed_sections: list[str],
) -> tuple[dict[str, str], str]:
    """统一 section 更新前的预处理。"""
    normalized = content.replace("\\n", "\n")
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    sections = _parse_section_file(file_path, allowed_sections)
    return sections, normalized


def _read_title(file_path: str, default_title: str = "# 标题") -> str:
    """读取文件的标题行（第一行）。

    Args:
        file_path: 文件路径
        default_title: 默认标题，文件不存在或第一行不是标题时使用

    Returns:
        标题行内容
    """
    if not os.path.exists(file_path):
        return default_title
    with open(file_path, "r", encoding="utf-8") as f:
        first_line = f.readline().strip()
    if first_line.startswith("# "):
        return first_line
    return default_title


def mark_event_triggered(agent_name: str, event_name: str) -> str:
    """将 status.md 中【event_name】对应行的 [ ] 精准替换为 [x]。

    Args:
        agent_name: 角色名称
        event_name: 事件名称（不含【】括号，如"破绽初现"）

    Returns:
        操作结果描述
    """
    status_path = character_path(agent_name, "status.md")
    if not os.path.exists(status_path):
        return "status.md 不存在"

    content = Path(status_path).read_text(encoding="utf-8")
    pattern = rf"- \[ \] (【{re.escape(event_name)}】)"
    new_content, count = re.subn(pattern, r"- [x] \1", content)

    if count == 0:
        return f"未找到事件【{event_name}】"

    Path(status_path).write_text(new_content, encoding="utf-8")
    return f"已标记【{event_name}】为已触发"


def _update_section_file(
    file_path: str,
    section: str,
    content: str,
    allowed_sections: list[str],
    title_line: str,
) -> str:
    """覆盖更新 section 文件中的指定字段。

    Args:
        file_path: 文件路径
        section: 要更新的 section 名称
        content: 新内容
        allowed_sections: 允许的 section 白名单
        title_line: 文件标题行

    Returns:
        更新结果描述
    """
    sections, normalized = _prepare_section_update(
        file_path, content, allowed_sections
    )

    if section not in allowed_sections:
        return f"字段 {section} 不在白名单中"

    sections[section] = normalized
    _write_section_file(file_path, sections, allowed_sections, title_line)
    return f"已更新 {section}"


def _append_section_file(
    file_path: str,
    section: str,
    content: str,
    allowed_sections: list[str],
    title_line: str,
) -> str:
    """追加内容到 section 文件的指定字段（保留原有内容）。

    Args:
        file_path: 文件路径
        section: 要追加的 section 名称
        content: 要追加的内容
        allowed_sections: 允许的 section 白名单
        title_line: 文件标题行

    Returns:
        更新结果描述
    """
    sections, normalized = _prepare_section_update(
        file_path, content, allowed_sections
    )

    if section not in allowed_sections:
        return f"字段 {section} 不在白名单中"

    existing = sections.get(section, _EMPTY_PLACEHOLDER)
    if existing == _EMPTY_PLACEHOLDER:
        sections[section] = normalized
    else:
        sections[section] = existing + "\n\n" + normalized

    _write_section_file(file_path, sections, allowed_sections, title_line)
    return f"已追加到 {section}"


def read_growth_entries(agent_name: str) -> dict[str, str]:
    """读取 growth.md，返回 {id: content} 字典。

    格式：[P001] 内容（支持多行）
    文件不存在返回空字典。
    """
    path = Path(character_path(agent_name, "growth.md"))
    if not path.exists():
        return {}

    content = path.read_text(encoding="utf-8")
    entries: dict[str, str] = {}
    pattern = r"\[(\w+)\]\s*(.+?)(?=\n\[|$)"
    for m in re.finditer(pattern, content, re.DOTALL):
        entries[m.group(1)] = m.group(2).strip()
    return entries


def write_growth_entries(agent_name: str, entries: dict[str, str]) -> None:
    """将 {id: content} 字典写回 growth.md，按 ID 数字部分排序。"""
    path = Path(character_path(agent_name, "growth.md"))
    def _sort_key(value: str) -> int:
        try:
            return int(re.sub(r"[^0-9]", "", value))
        except ValueError:
            return 0

    sorted_ids = sorted(entries.keys(), key=_sort_key)

    lines = [_GROWTH_TITLE, ""]
    for entry_id in sorted_ids:
        lines.append(f"[{entry_id}] {entries[entry_id]}")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def load_growth_for_prompt(agent_name: str, default: str = "（尚无人格沉淀）") -> str:
    """加载 growth.md 用于 prompt 注入。

    返回完整内容（去掉所有空行），如果只有标题或为空则返回默认值。
    """
    path = Path(character_path(agent_name, "growth.md"))
    if not path.exists():
        return default

    content = path.read_text(encoding="utf-8").strip()
    if not content or content == _GROWTH_TITLE:
        return default

    # 去掉所有空行
    lines = [line for line in content.split("\n") if line.strip()]
    return "\n".join(lines)


# ===== 通用文件读取 =====


def load_text(path: Path) -> str:
    """读取文本文件内容，文件不存在返回空字符串。"""
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def read_agent_file(agent_name: str, filename: str) -> str:
    """读取角色目录下的指定文件内容，不存在返回空字符串。"""
    path = character_path(agent_name, filename)
    return load_text(Path(path))


def read_file_tail(file_path: str | Path, lines: int = 10) -> str:
    """读取文件最后 N 个非空行。

    Args:
        file_path: 文件路径
        lines: 需要读取的行数

    Returns:
        最后 N 个非空行拼接的字符串
    """
    path = Path(file_path)
    if not path.exists():
        return ""

    with open(path, "r", encoding="utf-8") as f:
        all_lines = f.readlines()

    content_lines = [line for line in all_lines if line.strip()]
    tail_lines = content_lines[-lines:] if len(content_lines) >= lines else content_lines
    return "".join(tail_lines).strip()


# ===== 备份功能 =====


def cleanup_old_backups(bak_dir: Path, pattern: str, max_count: int = 10) -> int:
    """清理旧备份文件，只保留最近的 max_count 个。

    Args:
        bak_dir: 备份目录路径
        pattern: 文件匹配模式，如 "Memory_*_pre.md"
        max_count: 最大保留数量，默认 10

    Returns:
        删除的文件数量
    """
    bak_files = sorted(
        bak_dir.glob(pattern),
        key=lambda f: f.stat().st_mtime,
    )
    deleted = 0
    if len(bak_files) > max_count:
        for old_bak in bak_files[:-max_count]:
            old_bak.unlink()
            deleted += 1
    return deleted


def backup_file(src: Path, agent_name: str, prefix: str, max_backups: int = 10) -> Path:
    """备份文件到 agent 的 bak 目录，保留最近 max_backups 个备份。

    Args:
        src: 源文件路径
        agent_name: 角色名
        prefix: 备份文件前缀，如 "Memory"、"user"
        max_backups: 最大保留备份数，默认 10

    Returns:
        备份文件的完整路径
    """
    bak_dir = Path(character_path(agent_name, "bak"))
    bak_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    bak_path = bak_dir / f"{prefix}_{ts}_pre.md"
    shutil.copy2(src, bak_path)

    cleanup_old_backups(bak_dir, f"{prefix}_*_pre.md", max_count=max_backups)
    return bak_path


# ===== JSON 状态文件 =====


def get_consolidation_state_path(agent_name: str) -> Path:
    """获取整合进度文件路径。"""
    return Path(character_path(agent_name, ".consolidation_state.json"))


def load_consolidation_state(agent_name: str) -> Optional[str]:
    """读取上次整合到的日期，返回如 '2月10日' 或 None。

    Args:
        agent_name: 角色名

    Returns:
        最后整合的日期字符串，或 None（无记录/解析失败）
    """
    p = get_consolidation_state_path(agent_name)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data.get("last_consolidated_date")


def save_consolidation_state(agent_name: str, last_date: str) -> None:
    """保存整合进度。

    Args:
        agent_name: 角色名
        last_date: 最后整合的日期字符串，如 '2月10日'
    """
    p = get_consolidation_state_path(agent_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"last_consolidated_date": last_date}, ensure_ascii=False),
        encoding="utf-8",
    )


def load_last_memory_size(agent_name: str) -> Optional[int]:
    """读取上次整合时的 memory.md 文件大小（字节）。

    Args:
        agent_name: 角色名

    Returns:
        上次整合时的文件大小，或 None（无记录）
    """
    p = get_consolidation_state_path(agent_name)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data.get("last_memory_size")


def save_memory_size(agent_name: str, size: int) -> None:
    """保存当前 memory.md 的文件大小。

    Args:
        agent_name: 角色名
        size: 文件大小（字节）
    """
    p = get_consolidation_state_path(agent_name)
    p.parent.mkdir(parents=True, exist_ok=True)

    # 读取现有状态，保留 last_consolidated_date
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    data["last_memory_size"] = size
    p.write_text(
        json.dumps(data, ensure_ascii=False),
        encoding="utf-8",
    )


# ===== 安全写回（带并发保护） =====


def safe_write_memory(
    path: Path,
    sections: dict[str, str],
    agent_name: str,
    original_content: str,
) -> int:
    """安全写回 memory.md，带最小并发保护。

    策略：
    - 若 current 以 original 开头，视为尾部追加，保留该追加；
    - 若 current == original，正常覆盖；
    - 否则判定为中间变更，放弃写回并返回 -1。

    Args:
        path: 文件路径
        sections: 按日期组织的记忆内容 {日期: 内容}
        agent_name: 角色名（用于日志）
        original_content: 原始内容（用于检测并发变更）

    Returns:
        写入后的文件长度，-1 表示检测到并发冲突放弃写回
    """
    from log_config.routing import routing_logger

    current_content = path.read_text(encoding="utf-8")

    if current_content.startswith(original_content):
        appended = current_content[len(original_content) :]
        if appended:
            routing_logger.info(
                f"[整理器] {agent_name} 检测到并发尾部追加 ({len(appended)} 字符)，将保留"
            )
    elif current_content == original_content:
        appended = ""
    else:
        routing_logger.warning(
            f"[整理器] {agent_name} 检测到并发中间变更，已放弃写回以避免覆盖（建议稍后重试）"
        )
        return -1

    parts = [f"# {agent_name} 的长期记忆", ""]
    for date, body in sections.items():
        parts.append(f"## {date}")
        parts.append(body.strip())
        parts.append("")
    result = "\n".join(parts).strip() + "\n"

    # 追加并发期间新写入的内容
    if appended:
        result += appended

    path.write_text(result, encoding="utf-8")
    return len(result)
