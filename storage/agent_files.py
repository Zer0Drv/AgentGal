"""角色目录文件操作 - 读写 soul/memory/status/user/growth/sidecar 等文件"""

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Literal, TypedDict

from shared.config import character_path
from log_config.routing import routing_logger

_EMPTY_PLACEHOLDER = "（暂无）"
_GROWTH_TITLE = "# 心路历程"


_FileUpdateOperation = Literal["append", "replace", "remove", "add", "skip"]


class _FileUpdateResultBase(TypedDict):
    file: str
    target: str
    operation: _FileUpdateOperation


class FileUpdateResult(_FileUpdateResultBase, total=False):
    before: str
    after: str
    appended: str
    added: str
    removed: str
    reason: str


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


def get_fields_from_file(file_path: str) -> list[str] | None:
    """从文件中提取 ## 标题列表，文件不存在返回 None。"""
    if not os.path.exists(file_path):
        return None
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    return [line[3:].strip() for line in content.split("\n") if line.startswith("## ")]


def get_allowed_fields(agent_name: str, file_type: str) -> list[str]:
    """获取允许字段列表（文件驱动）。

    Args:
        agent_name: 角色名
        file_type: "status" | "user"

    Returns:
        文件中所有 ## 标题列表，文件不存在则返回 [] 并记录警告
    """
    file_path = character_path(agent_name, f"{file_type}.md")
    fields = get_fields_from_file(file_path)
    if fields is None:
        routing_logger.warning(f"[{agent_name}] {file_type}.md 不存在，无法获取字段白名单")
        return []
    return fields


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
    *,
    fill_missing_sections: bool = True,
) -> dict[str, str]:
    """解析 section 式文件，返回 {字段名: 内容} 字典。"""
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

    if fill_missing_sections:
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
    *,
    fill_missing_sections: bool = True,
) -> tuple[dict[str, str], str]:
    """section 更新前的统一预处理：规范化内容 + 读取现有 sections。"""
    normalized = content.replace("\\n", "\n")
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    sections = _parse_section_file(file_path, allowed_sections, fill_missing_sections=fill_missing_sections)
    return sections, normalized


def _update_section_file(
    file_path: str,
    section: str,
    content: str,
    allowed_sections: list[str],
    title_line: str,
    *,
    append: bool = False,
    fill_missing_sections: bool = True,
) -> FileUpdateResult:
    """更新 section 文件中的指定字段。

    Args:
        file_path: 文件路径
        section: 要更新的 section 名称
        content: 新内容
        allowed_sections: 允许的 section 白名单
        title_line: 文件标题行
        append: True 时追加到已有内容后，False 时覆盖

    Returns:
        可序列化的更新结果，供日志记录具体写回内容
    """
    sections, normalized = _prepare_section_update(
        file_path,
        content,
        allowed_sections,
        fill_missing_sections=fill_missing_sections,
    )
    file_name = Path(file_path).name

    if section not in allowed_sections:
        return FileUpdateResult(
            file=file_name,
            target=section,
            operation="skip",
            reason=f"字段 {section} 不在白名单中",
        )

    existing = sections.get(section, _EMPTY_PLACEHOLDER)
    if append:
        if existing == _EMPTY_PLACEHOLDER:
            sections[section] = normalized
        else:
            sections[section] = existing + "\n\n" + normalized
    else:
        sections[section] = normalized

    _write_section_file(file_path, sections, allowed_sections, title_line)
    if append:
        return FileUpdateResult(
            file=file_name,
            target=section,
            operation="append",
            appended=normalized,
        )
    return FileUpdateResult(
        file=file_name,
        target=section,
        operation="replace",
        before=existing,
        after=normalized,
    )


def _bootstrap_section_file_from_source(
    source_path: str,
    target_path: str,
    allowed_sections: list[str],
    title_line: str,
) -> None:
    """若 target 不存在，则用 source 的当前内容初始化；source 不存在时写空骨架。"""
    if os.path.exists(target_path):
        return

    if os.path.exists(source_path):
        shutil.copy2(source_path, target_path)
        return

    sections = {field: _EMPTY_PLACEHOLDER for field in allowed_sections}
    _write_section_file(target_path, sections, allowed_sections, title_line)


# ===== 事件队列操作（status.md 中的打算 / 待触发事件） =====


def mark_event_triggered(
    agent_name: str,
    event_name: str,
    section_name: str = "待触发事件",
) -> FileUpdateResult:
    """将 status.md 中指定 section 里【event_name】对应的未触发事件行直接删除。

    Args:
        agent_name: 角色名称
        event_name: 事件名称（不含【】括号，如"破绽初现"）
        section_name: 要操作的 section 标题，默认 "待触发事件"，角色传 "打算"

    Returns:
        可序列化的更新结果，供日志记录具体写回内容
    """
    status_path = character_path(agent_name, "status.md")
    try:
        content = Path(status_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return FileUpdateResult(
            file="status.md", target=section_name, operation="skip", reason="status.md 不存在"
        )
    pattern = rf"- \[ \] 【{re.escape(event_name)}】[^\n]*\n?"
    matches = list(re.finditer(pattern, content))

    if not matches:
        return FileUpdateResult(
            file="status.md",
            target=section_name,
            operation="skip",
            reason=f"未找到事件【{event_name}】",
        )

    removed = "\n".join(m.group(0).rstrip("\n") for m in matches)
    # Build new_content by splicing out matched spans (avoids a second regex scan)
    parts: list[str] = []
    pos = 0
    for m in matches:
        parts.append(content[pos : m.start()])
        pos = m.end()
    parts.append(content[pos:])
    new_content = "".join(parts)

    Path(status_path).write_text(new_content, encoding="utf-8")
    return FileUpdateResult(
        file="status.md", target=section_name, operation="remove", removed=removed
    )


def add_pending_event(
    agent_name: str,
    event_line: str,
    section_name: str = "待触发事件",
) -> FileUpdateResult:
    """在 status.md 的指定区块中，于第一个未触发条目前插入新条目。
    插入前检查同名条目是否已存在（按【】内名称匹配），存在则跳过。

    Args:
        agent_name: 角色名称
        event_line: 新条目描述（如"【名称】描述文字"，函数会自动补 `- [ ] ` 前缀）
        section_name: 要操作的 section 标题，默认 "待触发事件"，角色传 "打算"

    Returns:
        可序列化的更新结果，供日志记录具体写回内容
    """
    status_path = character_path(agent_name, "status.md")
    stripped = event_line.strip()
    if not stripped.startswith("- [ ]"):
        stripped = f"- [ ] {stripped}"

    try:
        content = Path(status_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return FileUpdateResult(
            file="status.md", target=section_name, operation="skip", reason="status.md 不存在"
        )

    name_match = re.search(r"【(.+?)】", stripped)
    if name_match:
        event_name = name_match.group(1)
        if re.search(rf"【{re.escape(event_name)}】", content):
            return FileUpdateResult(
                file="status.md",
                target=section_name,
                operation="skip",
                reason=f"【{event_name}】已存在，跳过",
            )

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
    return FileUpdateResult(
        file="status.md", target=section_name, operation="add", added=stripped
    )


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
    sorted_ids = sorted(entries.keys(), key=_growth_id_sort_key)

    lines = [_GROWTH_TITLE, ""]
    for entry_id in sorted_ids:
        lines.append(f"[{entry_id}] {entries[entry_id]}")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")



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


# ===== Agent 响应写回（memory / status / user） =====


def _parse_memory_entries(text: str) -> list[str]:
    """将 memory.md 文本切分为独立条目列表。"""
    entries, current = [], []
    for line in text.split("\n"):
        if line.strip().startswith("##") or (line.strip().startswith("-") and "**" in line):
            if current:
                entries.append("\n".join(current).strip())
            current = [line]
        elif line.strip() or current:
            current.append(line)
    if current:
        entries.append("\n".join(current).strip())
    return entries


def _growth_id_sort_key(value: str) -> int:
    """提取 growth ID 中的数字部分用于排序，无数字时返回 0。"""
    try:
        return int(re.sub(r"[^0-9]", "", value))
    except ValueError:
        return 0


def update_memory(agent_name: str, memory_content: str) -> FileUpdateResult:
    """追加 memory 内容到 memory.md（带去重）。"""
    if not memory_content or not memory_content.strip():
        return FileUpdateResult(
            file="memory.md", target="长期记忆", operation="skip", reason="内容为空，跳过"
        )

    memory_path = character_path(agent_name, "memory.md")
    os.makedirs(os.path.dirname(memory_path), exist_ok=True)
    clean = memory_content.replace("\\n", "\n").strip()

    try:
        existing = Path(memory_path).read_text(encoding="utf-8")
    except OSError:
        existing = ""
    existing_set = set(_parse_memory_entries(existing))
    unique = [e for e in _parse_memory_entries(clean) if e and e not in existing_set]

    if not unique:
        return FileUpdateResult(
            file="memory.md",
            target="长期记忆",
            operation="skip",
            reason="所有 entry 已存在，跳过",
        )

    to_append = "\n\n".join(unique)
    if existing.strip():
        with open(memory_path, "a", encoding="utf-8") as f:
            f.write(f"\n\n{to_append}")
    else:
        with open(memory_path, "w", encoding="utf-8") as f:
            f.write(f"# {agent_name} 的长期记忆\n\n{to_append}")

    return FileUpdateResult(
        file="memory.md", target="长期记忆", operation="append", appended=to_append
    )


def update_status(agent_name: str, field: str, content: str) -> FileUpdateResult:
    """覆盖更新 status.md 的指定字段。"""
    # 事件队列只能通过 add_pending_event/mark_event_triggered 操作，禁止整体覆盖
    if agent_name != "narrator" and field == "打算":
        routing_logger.warning(f"[{agent_name}] 禁止通过 <status> 覆盖「打算」字段，请使用 <triggered>/<add_event>")
        return FileUpdateResult(
            file="status.md",
            target=field,
            operation="skip",
            reason="禁止覆盖「打算」字段，请用 <triggered>/<add_event> 逐条管理",
        )
    if agent_name == "narrator" and field == "待触发事件":
        routing_logger.warning("[narrator] 禁止通过 <status> 覆盖「待触发事件」字段，请使用 <triggered>/<add_event>")
        return FileUpdateResult(
            file="status.md",
            target=field,
            operation="skip",
            reason="禁止覆盖「待触发事件」字段，请用 <triggered>/<add_event> 逐条管理",
        )
    allowed = get_allowed_fields(agent_name, "status")
    status_path = character_path(agent_name, "status.md")
    return _update_section_file(status_path, field, content, allowed, _read_title(status_path, "# 我的状态"))


_RELATIONS_TITLE = "# 我眼中的其他人"


def read_relations(agent_name: str) -> dict[str, str]:
    """读取 relations.md 为 {target: content}，保留原顺序；文件不存在返回空字典。"""
    path = character_path(agent_name, "relations.md")
    if not os.path.exists(path):
        return {}
    return _parse_section_file(path, [], fill_missing_sections=False)


def read_relations_section(agent_name: str, target: str) -> str:
    """读取 relations.md 中 `## {target}` section 的内容，缺失返回空字符串。"""
    return read_relations(agent_name).get(target, "")


def update_relations(agent_name: str, target: str, content: str) -> FileUpdateResult:
    """覆盖或新增 relations.md 中的 `## {target}` section。

    与 status/user 不同，relations 没有固定白名单 —— target 是动态角色名。
    """
    target_clean = target.strip()
    if not target_clean:
        return FileUpdateResult(
            file="relations.md", target=target, operation="skip", reason="target 为空"
        )
    normalized = content.replace("\\n", "\n").strip()
    if not normalized:
        return FileUpdateResult(
            file="relations.md", target=target_clean, operation="skip", reason="内容为空，跳过"
        )

    path = character_path(agent_name, "relations.md")
    sections = read_relations(agent_name)
    before = sections.get(target_clean, "")
    sections[target_clean] = normalized

    title = _read_title(path, _RELATIONS_TITLE)
    _write_section_file(path, sections, list(sections.keys()), title)

    if before:
        return FileUpdateResult(
            file="relations.md",
            target=target_clean,
            operation="replace",
            before=before,
            after=normalized,
        )
    return FileUpdateResult(
        file="relations.md",
        target=target_clean,
        operation="append",
        appended=normalized,
    )


def update_player(agent_name: str, field: str, content: str) -> FileUpdateResult:
    """追加更新 tmp_user.md 的指定字段。

    tmp_user.md 是 user.md 的工作草稿：首次写入时先复制正式档案，再叠加本轮增量。
    """
    allowed = get_allowed_fields(agent_name, "user")
    user_path = character_path(agent_name, "user.md")
    tmp_path = character_path(agent_name, "tmp_user.md")
    title_line = _read_title(user_path, "# 玩家档案")
    _bootstrap_section_file_from_source(user_path, tmp_path, allowed, title_line)
    return _update_section_file(
        tmp_path,
        field,
        content,
        allowed,
        title_line,
        append=True,
        fill_missing_sections=True,
    )
