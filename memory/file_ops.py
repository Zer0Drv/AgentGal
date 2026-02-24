"""记忆系统文件操作 - 底层文件操作函数

注意：当前 Agent 通过 XML 解析响应手动触发更新，
而非通过 Tool 调用。本模块保留底层文件操作供其他模块使用。
"""

import os

from engine.config import character_path

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
            sections[field] = "（暂无）"

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


def _update_section_file(
    file_path: str,
    section: str,
    content: str,
    allowed_sections: list[str],
    title_line: str,
) -> str:
    """覆盖模式更新 section 式文件（用于 update_status）。"""
    if section not in allowed_sections:
        return (
            f"错误：不允许的字段「{section}」。"
            f"只能更新以下字段：{', '.join(allowed_sections)}"
        )

    content = content.replace("\\n", "\n")
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    sections = _parse_section_file(file_path, allowed_sections)

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
        return (
            f"错误：不允许的字段「{section}」。"
            f"只能更新以下字段：{', '.join(allowed_sections)}"
        )

    content = content.replace("\\n", "\n")
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    sections = _parse_section_file(file_path, allowed_sections)
    old_content = sections.get(section, "").strip()

    # 如果字段为空或占位符，直接写入
    if not old_content or old_content == "（暂无）":
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
