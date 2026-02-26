"""记忆系统文件操作 - 底层文件操作函数

注意：当前 Agent 通过 XML 解析响应手动触发更新，
而非通过 Tool 调用。本模块保留底层文件操作供其他模块使用。
"""

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from engine.config import character_path

_EMPTY_PLACEHOLDER = "（暂无）"
_GROWTH_TITLE = "# 人格沉淀层"

# 每个角色 status.md 允许的字段白名单（对应 ## 标题）
STATUS_FIELDS: dict[str, list[str]] = {
    "lilith": ["身份", "心境", "我和他", "在意的事", "打算"],
    "mitsuki": ["心境", "我和他", "在意的事", "打算"],
    "narrator": ["故事阶段", "当前时间", "场景", "正在发酵的冲突", "伏笔"],
}

# 每个角色 user.md 允许的字段白名单
USER_FIELDS: dict[str, list[str]] = {
    "lilith": ["基本信息", "观察到的特质", "互动模式"],
    "mitsuki": ["基本信息", "观察到的特质", "互动模式"],
    "narrator": ["玩家风格", "关键选择", "当前倾向"],
}


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
    section_order: list[str] = []
    current_sec = None
    current_lines: list[str] = []

    for line in existing_content.split("\n"):
        if line.startswith("## "):
            if current_sec:
                sections[current_sec] = "\n".join(current_lines).strip()
            current_sec = line[3:].strip()
            if current_sec not in section_order:
                section_order.append(current_sec)
            current_lines = []
        elif not line.startswith("# "):
            current_lines.append(line)

    if current_sec:
        sections[current_sec] = "\n".join(current_lines).strip()

    for field in allowed_sections:
        if field not in section_order:
            section_order.append(field)
            sections[field] = _EMPTY_PLACEHOLDER

    return sections


def _invalid_section_message(section: str, allowed_sections: list[str]) -> str:
    return (
        f"错误：不允许的字段「{section}」。"
        f"只能更新以下字段：{', '.join(allowed_sections)}"
    )


def _numeric_suffix_sort_key(value: str) -> int:
    """提取字符串中的数字部分用于排序，无法解析时返回 0。"""
    try:
        return int(re.sub(r"[^0-9]", "", value))
    except ValueError:
        return 0


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


def _update_section_file(
    file_path: str,
    section: str,
    content: str,
    allowed_sections: list[str],
    title_line: str,
) -> str:
    """覆盖模式更新 section 式文件（用于 update_status）。"""
    if section not in allowed_sections:
        return _invalid_section_message(section, allowed_sections)

    sections, content = _prepare_section_update(file_path, content, allowed_sections)

    old_content = sections.get(section, "").strip()
    new_content = content.strip()
    if old_content == new_content:
        return f"[{section}] 内容未变化，无需更新。"

    sections[section] = content
    _write_section_file(file_path, sections, allowed_sections, title_line)
    return f"已更新 [{section}]: {content[:50]}..."


def _append_section_file(
    file_path: str,
    section: str,
    content: str,
    allowed_sections: list[str],
    title_line: str,
) -> str:
    """追加模式更新 section 式文件（用于 update_player）。

    只追加新信息，已存在的内容自动跳过。
    """
    if section not in allowed_sections:
        return _invalid_section_message(section, allowed_sections)

    sections, content = _prepare_section_update(file_path, content, allowed_sections)
    old_content = sections.get(section, "").strip()

    # 如果字段为空或占位符，直接写入
    if not old_content or old_content == _EMPTY_PLACEHOLDER:
        sections[section] = content.strip()
        _write_section_file(file_path, sections, allowed_sections, title_line)
        return f"已更新 [{section}]: {content[:50]}..."

    # 去重：新内容已是已有内容的子串 → 跳过
    new_stripped = content.strip()
    if new_stripped in old_content:
        return f"[{section}] 内容已存在，无需重复追加。"

    # 追加到末尾（换行分隔）
    sections[section] = old_content + "\n" + new_stripped
    _write_section_file(file_path, sections, allowed_sections, title_line)
    return f"已追加 [{section}]: {content[:50]}..."


def _read_title(file_path: str, default: str) -> str:
    """读取文件首行标题，不存在则返回默认值。"""
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
            if first_line.startswith("# "):
                return first_line
    return default


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
    sorted_ids = sorted(entries.keys(), key=_numeric_suffix_sort_key)

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


def _load_json_file(path: Path) -> Optional[dict]:
    """读取 JSON 文件，失败返回 None。"""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_json_file(path: Path, data: dict) -> None:
    """写入 JSON 文件（UTF-8）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_consolidation_state(agent_name: str) -> Optional[str]:
    """读取上次整合到的日期，返回如 '2月10日' 或 None。

    Args:
        agent_name: 角色名

    Returns:
        最后整合的日期字符串，或 None（无记录/解析失败）
    """
    p = get_consolidation_state_path(agent_name)
    data = _load_json_file(p)
    if data is None:
        return None
    return data.get("last_consolidated_date")


def save_consolidation_state(agent_name: str, last_date: str) -> None:
    """保存整合进度。

    Args:
        agent_name: 角色名
        last_date: 最后整合的日期字符串，如 '2月10日'
    """
    p = get_consolidation_state_path(agent_name)
    _save_json_file(p, {"last_consolidated_date": last_date})


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
