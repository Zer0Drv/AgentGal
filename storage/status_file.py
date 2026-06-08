"""status.md section 文件引擎：字段白名单 + 事件队列 + 状态写回。

机制层：负责 status.md 的 section 解析/写回、打算/待触发事件队列维护，
以及"禁止整体覆盖打算/待触发事件字段"的业务规则。
"""

import os
import re
import shutil
from pathlib import Path
from typing import Literal, TypedDict

from log_config.routing import routing_logger
from models import status_fields
from shared.config import character_path

_EMPTY_PLACEHOLDER = "（暂无）"


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
) -> FileUpdateResult | None:
    """将 status.md 中指定 section 里【event_name】对应的未触发事件行直接删除。

    出错时记录日志并返回 None（吸收原 Repository 包装的容错层）。

    Args:
        agent_name: 角色名称
        event_name: 事件名称（不含【】括号，如"破绽初现"）
        section_name: 要操作的 section 标题，默认 "待触发事件"，角色传 "打算"

    Returns:
        可序列化的更新结果（出错时为 None），供日志记录具体写回内容
    """
    status_path = character_path(agent_name, "status.md")
    try:
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
    except Exception as e:
        routing_logger.error(f"[{agent_name}] mark_event_triggered 失败: {e}")
        return None


def add_pending_event(
    agent_name: str,
    event_line: str,
    section_name: str = "待触发事件",
) -> FileUpdateResult | None:
    """在 status.md 的指定区块中，于第一个未触发条目前插入新条目。

    event_line 为哨兵「无」时跳过（返回 None）；插入前按【】内名称去重，已存在则跳过。
    出错时记录日志并返回 None（吸收原 Repository 包装的容错层）。

    Args:
        agent_name: 角色名称
        event_line: 新条目描述（如"【名称】描述文字"，函数会自动补 `- [ ] ` 前缀）
        section_name: 要操作的 section 标题，默认 "待触发事件"，角色传 "打算"

    Returns:
        可序列化的更新结果（哨兵/出错时为 None），供日志记录具体写回内容
    """
    if event_line.strip() == "无":
        return None
    status_path = character_path(agent_name, "status.md")
    stripped = event_line.strip()
    if not stripped.startswith("- [ ]"):
        stripped = f"- [ ] {stripped}"

    try:
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
    except Exception as e:
        routing_logger.error(f"[{agent_name}] add_pending_event 失败: {e}")
        return None


# ===== Agent 状态写回（status.md） =====


def update_status(agent_name: str, field: str, content: str) -> FileUpdateResult:
    """覆盖更新 status.md 的指定字段。"""
    # 事件段只能逐条经 add_pending_event/mark_event_triggered 维护，禁止整段覆盖
    if field in status_fields.EVENT_SECTIONS:
        routing_logger.warning(
            f"[{agent_name}] 禁止通过 <status> 覆盖事件段「{field}」，请使用 <triggered>/<add_event>"
        )
        return FileUpdateResult(
            file="status.md",
            target=field,
            operation="skip",
            reason=f"禁止覆盖事件段「{field}」，请用 <triggered>/<add_event> 逐条管理",
        )
    allowed = get_allowed_fields(agent_name, "status")
    status_path = character_path(agent_name, "status.md")
    return _update_section_file(status_path, field, content, allowed, _read_title(status_path, "# 我的状态"))


def update_status_allow_new_field(agent_name: str, field: str, content: str) -> FileUpdateResult:
    """覆盖更新 status.md 字段；字段不存在时追加为新 section。

    仅用于程序派生字段。LLM 输出仍应走 update_status() 的文件白名单。
    """
    allowed = get_allowed_fields(agent_name, "status")
    if field not in allowed:
        allowed = [*allowed, field]
    status_path = character_path(agent_name, "status.md")
    return _update_section_file(
        status_path,
        field,
        content,
        allowed,
        _read_title(status_path, "# 我的状态"),
    )
