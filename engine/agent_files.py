"""角色目录文件操作 - 读写 soul/memory/status/user/growth/sidecar 等文件"""

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from engine.config import character_path

_EMPTY_PLACEHOLDER = "（暂无）"
_GROWTH_TITLE = "# 心路历程"

# 每个角色 status.md 允许的字段白名单（对应 ## 标题），文件不存在时的回退默认值
STATUS_FIELDS: dict[str, list[str]] = {
    "lilith": ["身份", "心境", "我和他", "在意的事", "打算"],
    "mitsuki": ["心境", "我和他", "在意的事", "打算"],
    "narrator": ["关系现状", "当前时间", "场景", "叙事焦点", "待触发事件"],
}

# 每个角色 user.md 允许的字段白名单，文件不存在时的回退默认值
USER_FIELDS: dict[str, list[str]] = {
    "lilith": ["基本信息", "观察到的特质", "互动模式"],
    "mitsuki": ["基本信息", "观察到的特质", "互动模式"],
}


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


# ===== JSON sidecar =====


def read_sidecar_json(agent_name: str, filename: str) -> dict:
    """读取角色目录下的 JSON sidecar 文件，解析失败返回空字典。"""
    path = Path(character_path(agent_name, filename))
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def write_sidecar_json(agent_name: str, filename: str, data: dict) -> None:
    """将 data 写入角色目录下的 JSON sidecar 文件。"""
    path = Path(character_path(agent_name, filename))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ===== 字段白名单 =====


def _get_fields_from_file(file_path: str) -> list[str] | None:
    """从文件中提取 ## 标题列表，文件不存在返回 None。"""
    if not os.path.exists(file_path):
        return None
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    return [line[3:].strip() for line in content.split("\n") if line.startswith("## ")]


def get_allowed_fields(agent_name: str, file_type: str) -> list[str]:
    """获取允许字段列表（文件驱动，失败回退到默认值）。

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
    defaults = STATUS_FIELDS if file_type == "status" else USER_FIELDS
    return defaults.get(agent_name, [])


# ===== Section 文件读写 =====


def _read_title(file_path: str, default_title: str = "# 标题") -> str:
    """读取文件的标题行（第一行），文件不存在或非标题行时返回 default_title。"""
    if not os.path.exists(file_path):
        return default_title
    with open(file_path, "r", encoding="utf-8") as f:
        first_line = f.readline().strip()
    if first_line.startswith("# "):
        return first_line
    return default_title


def _parse_section_file(
    file_path: str,
    allowed_sections: list[str],
) -> dict[str, str]:
    """解析 section 式文件，返回 {字段名: 内容} 字典，自动补全白名单字段。"""
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
    """section 更新前的统一预处理：规范化内容 + 读取现有 sections。"""
    normalized = content.replace("\\n", "\n")
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    sections = _parse_section_file(file_path, allowed_sections)
    return sections, normalized


def _update_section_file(
    file_path: str,
    section: str,
    content: str,
    allowed_sections: list[str],
    title_line: str,
    *,
    append: bool = False,
) -> str:
    """更新 section 文件中的指定字段。

    Args:
        file_path: 文件路径
        section: 要更新的 section 名称
        content: 新内容
        allowed_sections: 允许的 section 白名单
        title_line: 文件标题行
        append: True 时追加到已有内容后，False 时覆盖

    Returns:
        更新结果描述
    """
    sections, normalized = _prepare_section_update(file_path, content, allowed_sections)

    if section not in allowed_sections:
        return f"字段 {section} 不在白名单中"

    if append:
        existing = sections.get(section, _EMPTY_PLACEHOLDER)
        if existing == _EMPTY_PLACEHOLDER:
            sections[section] = normalized
        else:
            sections[section] = existing + "\n\n" + normalized
    else:
        sections[section] = normalized

    _write_section_file(file_path, sections, allowed_sections, title_line)
    return f"已{'追加到' if append else '更新'} {section}"


# ===== 事件队列操作（status.md 中的打算 / 待触发事件） =====


def mark_event_triggered(agent_name: str, event_name: str, section_name: str = "待触发事件") -> str:
    """将 status.md 中指定 section 里【event_name】对应的未触发事件行直接删除。

    Args:
        agent_name: 角色名称
        event_name: 事件名称（不含【】括号，如"破绽初现"）
        section_name: 要操作的 section 标题，默认 "待触发事件"，角色传 "打算"

    Returns:
        操作结果描述
    """
    status_path = character_path(agent_name, "status.md")
    if not os.path.exists(status_path):
        return "status.md 不存在"

    content = Path(status_path).read_text(encoding="utf-8")
    pattern = rf"- \[ \] 【{re.escape(event_name)}】[^\n]*\n?"
    new_content, count = re.subn(pattern, "", content)

    if count == 0:
        return f"未找到事件【{event_name}】"

    Path(status_path).write_text(new_content, encoding="utf-8")
    return f"已完成并移除【{event_name}】"


def add_pending_event(agent_name: str, event_line: str, section_name: str = "待触发事件") -> str:
    """在 status.md 的指定区块中，于第一个未触发条目前插入新条目。
    插入前检查同名条目是否已存在（按【】内名称匹配），存在则跳过。

    Args:
        agent_name: 角色名称
        event_line: 新条目描述（如"【名称】描述文字"，函数会自动补 `- [ ] ` 前缀）
        section_name: 要操作的 section 标题，默认 "待触发事件"，角色传 "打算"

    Returns:
        操作结果描述
    """
    status_path = character_path(agent_name, "status.md")
    if not os.path.exists(status_path):
        return "status.md 不存在"

    stripped = event_line.strip()
    if not stripped.startswith("- [ ]"):
        stripped = f"- [ ] {stripped}"

    content = Path(status_path).read_text(encoding="utf-8")

    name_match = re.search(r"【(.+?)】", stripped)
    if name_match:
        event_name = name_match.group(1)
        if re.search(rf"【{re.escape(event_name)}】", content):
            return f"【{event_name}】已存在，跳过"

    lines = content.split("\n")

    in_section = False
    insert_idx = None
    for i, line in enumerate(lines):
        if line.startswith(f"## {section_name}"):
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                insert_idx = i
                break
            if re.match(r"- \[ \]", line):
                insert_idx = i
                break

    if insert_idx is None:
        lines.append(stripped)
    else:
        lines.insert(insert_idx, stripped)

    Path(status_path).write_text("\n".join(lines), encoding="utf-8")
    return f"已插入: {stripped}"


# ===== Growth 文件 =====


def read_growth_entries(agent_name: str) -> dict[str, str]:
    """读取 growth.md，返回 {id: content} 字典，文件不存在返回空字典。

    格式：[P001] 内容（支持多行）
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


# ===== 记忆文件安全写回（带并发保护） =====


def safe_write_memory(
    path: Path,
    sections: dict[str, str],
    agent_name: str,
    original_content: str,
) -> tuple[int, int]:
    """安全写回 memory.md，带最小并发保护。

    策略：
    - 若 current 以 original 开头，视为尾部追加，保留该追加；
    - 若 current == original，正常覆盖；
    - 否则判定为中间变更，放弃写回并返回 (-1, -1)。

    Args:
        path: 文件路径
        sections: 按日期组织的记忆内容 {日期: 内容}
        agent_name: 角色名（用于日志）
        original_content: 原始内容（用于检测并发变更）

    Returns:
        (写入后的文件长度, 已整理快照长度)，(-1, -1) 表示检测到并发冲突放弃写回
    """
    from log_config.routing import routing_logger
    from memory.parser import render_sections

    current_content = path.read_text(encoding="utf-8")

    if current_content.startswith(original_content):
        appended = current_content[len(original_content):]
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
        return -1, -1

    result = f"# {agent_name} 的长期记忆\n\n{render_sections(sections)}\n"
    consolidated_len = len(result)

    if appended:
        result += appended

    path.write_text(result, encoding="utf-8")
    return len(result), consolidated_len


# ===== 备份 =====


def cleanup_old_backups(bak_dir: Path, pattern: str, max_count: int = 10) -> int:
    """清理旧备份文件，只保留最近的 max_count 个，返回删除数量。"""
    bak_files = sorted(bak_dir.glob(pattern), key=lambda f: f.stat().st_mtime)
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
