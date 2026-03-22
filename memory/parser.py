"""memory.md 格式解析 - 文本规范化、事件切分、日期工具"""

import re
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

_EVENT_FIELD_PATTERN = r"^\s*(?:-\s*)?\*\*{field}\*\*：\s*(.*)$"


# ===== 文本规范化 =====


def normalize(content: str) -> str:
    """修复常见格式问题：字面\\n、日期标题不规范、多余空行。"""
    content = content.replace("\\n", "\n")
    content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
    lines = content.split("\n")
    out = []
    for line in lines:
        stripped = line.strip()
        m = re.match(r"^(?:#{1,2}\s*|\*\*)?(\d{1,2}月\d{1,2}日)(?:\*\*)?(?:\s.*)?$", stripped)
        if m:
            out.append(f"## {m.group(1)}")
        elif re.match(r"^\*\*(时间|地点|在场|关键词|重要度|内容)\*\*：", stripped):
            out.append(f"- {stripped}")
        else:
            out.append(line)
    content = "\n".join(out)
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content


# ===== 按日期切分 =====


def split_by_date(content: str) -> OrderedDict[str, str]:
    """按 ## X月X日 切分，同日期自动合并。

    Returns:
        sections: 日期 → 合并后的内容（保持出现顺序）
    """
    sections: OrderedDict[str, str] = OrderedDict()
    current_date = None
    current_lines: list[str] = []

    for line in content.split("\n"):
        m = re.match(r"^##\s*(\d{1,2}月\d{1,2}日)(?:\s.*)?$", line.strip())
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

    if current_date:
        body = "\n".join(current_lines).strip()
        if current_date in sections:
            sections[current_date] += "\n" + body
        else:
            sections[current_date] = body

    return sections


# ===== 按事件切分 =====


def split_events_raw(content: str) -> list[tuple[str | None, str]]:
    """按 - **时间**：字段分割内容为事件列表。

    格式：- **时间**：4月3日 上午

    Returns:
        [(日期, 事件内容), ...]，日期从时间字段的日期部分提取
    """
    time_pattern = re.compile(r"^(?:-\s*)?\*\*时间\*\*：(\d{1,2}月\d{1,2}日)")
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
    """将单日内容按事件分割为列表，无法识别时整体作为一个事件返回。"""
    events = [event_text for _, event_text in split_events_raw(day_content) if event_text]
    return events if events else [day_content.strip()]


# ===== 字段提取 =====


def extract_status_field(status_text: str, field_name: str) -> str:
    """从 status.md 文本中提取指定 ## 字段的值。

    Args:
        status_text: status.md 的完整文本内容
        field_name: 要提取的字段名（如 "叙事焦点"、"心境"）

    Returns:
        字段内容，未找到返回空字符串
    """
    pattern = re.compile(
        rf"^##\s+{re.escape(field_name)}\s*\n(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(status_text)
    if not m:
        return ""
    return m.group(1).strip()


def extract_game_date(text: str) -> str | None:
    """从文本中提取游戏日期（如 4月3日）。"""
    m = re.search(r"\*\*时间\*\*：\s*(\d{1,2}月\d{1,2}日)", text)
    if m:
        return m.group(1)
    return None


def extract_event_field(event_text: str, field_name: str) -> str:
    """从单个记忆事件块中提取指定字段的值。"""
    pattern = re.compile(
        _EVENT_FIELD_PATTERN.format(field=re.escape(field_name)),
        re.MULTILINE,
    )
    match = pattern.search(event_text or "")
    return match.group(1).strip() if match else ""


def parse_event_keywords(event_text: str) -> str:
    """解析单个事件块中的关键词字段。"""
    return extract_event_field(event_text, "关键词")


def parse_event_importance(event_text: str, default: int = 3) -> int:
    """解析单个事件块中的重要度字段；缺失或非法时回退默认值。"""
    raw = extract_event_field(event_text, "重要度")
    if not raw:
        return default
    match = re.search(r"\d+", raw)
    if not match:
        return default
    return max(1, min(5, int(match.group(0))))


# ===== 游戏日期工具 =====


def parse_cn_date(date_text: str) -> tuple[int, int] | None:
    """解析中文日期格式（如 4月3日 / 4月3日 08:00）为元组 (月, 日)。"""
    m = re.search(r"(\d{1,2})月(\d{1,2})日", (date_text or "").strip())
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    try:
        datetime(2000, month, day)
    except ValueError:
        return None
    return month, day


def canonical_cn_date(date_text: str) -> str | None:
    """将中文日期文本规范化为 X月X日。"""
    parsed = parse_cn_date(date_text)
    if parsed is None:
        return None
    month, day = parsed
    return f"{month}月{day}日"


def game_day_number(date_text: str) -> int | None:
    """将游戏日期映射到固定年份中的 day-of-year，便于比较天数差。"""
    parsed = parse_cn_date(date_text)
    if parsed is None:
        return None
    month, day = parsed
    return datetime(2000, month, day).timetuple().tm_yday


def game_day_diff(current_date: str, past_date: str) -> int | None:
    """返回两个游戏日期的天数差；若无法解析返回 None。"""
    current_day = game_day_number(current_date)
    past_day = game_day_number(past_date)
    if current_day is None or past_day is None:
        return None
    return max(current_day - past_day, 0)


def is_date_before(date_text: str, cutoff_date: str) -> bool:
    """判断 date_text 是否在 cutoff_date 之前。"""
    left = parse_cn_date(date_text)
    right = parse_cn_date(cutoff_date)
    if left is None or right is None:
        return False
    return left < right


# ===== sections 序列化 =====


def render_sections(sections: OrderedDict[str, str]) -> str:
    """将按日期组织的 sections 序列化为 memory.md Markdown 格式。逆操作为 split_by_date。"""
    parts: list[str] = []
    for date, body in sections.items():
        parts.append(f"## {date}")
        parts.append(body.strip())
        parts.append("")
    return "\n".join(parts).strip()


# ===== LLM 输出解析 =====


def parse_llm_memory_sections(
    llm_result: str, expected_dates: list[str] | None = None
) -> OrderedDict[str, str]:
    """解析整理器 step1 的 LLM 输出，容忍 LLM 生成的格式噪声（时间字段内含日期、无标题行等）。

    与 split_by_date 的区别：split_by_date 处理干净的 memory.md 文本；
    本函数处理 LLM 返回的同格式文本，会做额外的日期推断和 <analysis> 标签剥除。
    """
    cleaned = re.sub(r"<analysis>.*?</analysis>", "", llm_result, flags=re.DOTALL).strip()
    heading_pattern = re.compile(r"^\s*##\s*(\d{1,2}月\d{1,2}日)(?:\s.*)?$")
    time_pattern = re.compile(r"^\s*(?:-\s*)?\*\*时间\*\*：\s*(.*)$")
    field_pattern = re.compile(r"^\s*(?:-\s*)?\*\*(地点|在场|内容)\*\*：\s*(.*)$")
    explicit_date_pattern = re.compile(r"(\d{1,2}月\d{1,2}日)")
    fallback_date = expected_dates[0] if expected_dates and len(expected_dates) == 1 else None

    sections: OrderedDict[str, str] = OrderedDict()
    current_heading_date: str | None = None
    current_date: str | None = None
    current_lines: list[str] = []

    def flush_event() -> None:
        nonlocal current_date, current_lines
        if not current_date or not current_lines:
            current_date = None
            current_lines = []
            return
        event_text = "\n".join(current_lines).strip()
        if event_text:
            sections[current_date] = (
                sections.get(current_date, "") + ("\n\n" if current_date in sections else "") + event_text
            )
        current_date = None
        current_lines = []

    for line in cleaned.splitlines():
        heading_match = heading_pattern.match(line)
        if heading_match:
            current_heading_date = heading_match.group(1)
            continue

        time_match = time_pattern.match(line)
        if time_match:
            flush_event()
            time_value = time_match.group(1).strip()
            explicit_date_match = explicit_date_pattern.search(time_value)
            current_date = (
                explicit_date_match.group(1) if explicit_date_match else current_heading_date or fallback_date
            )
            if explicit_date_match:
                current_lines = [f"- **时间**：{time_value}"]
            elif current_date:
                current_lines = [f"- **时间**：{current_date} {time_value}".rstrip()]
            else:
                current_lines = [f"- **时间**：{time_value}"]
            continue

        if current_lines:
            field_match = field_pattern.match(line)
            if field_match:
                current_lines.append(f"- **{field_match.group(1)}**：{field_match.group(2).strip()}".rstrip())
            else:
                current_lines.append(line)

    flush_event()
    return sections


# ===== 安全写回（带并发保护） =====


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
