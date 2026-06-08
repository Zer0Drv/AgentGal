"""文本处理工具函数"""

import re

# =============================================================================
# 常量
# =============================================================================

_THINKING_PATTERN = re.compile(
    r"<thinking>.*?</thinking>|<think>.*?</think>|<reasoning>.*?</reasoning>",
    re.DOTALL,
)

_IDENTITY_PATTERN = re.compile(r"<identity>\s*(.+?)\s*</identity>", re.DOTALL)


# =============================================================================
# 工具函数
# =============================================================================


def strip_thinking(content: str) -> str:
    """移除 thinking/reasoning 标签"""
    return _THINKING_PATTERN.sub("", content)


def normalize_whitespace(content: str) -> str:
    """规范化空白字符：多个换行合并为两个，多个空格合并为一个"""
    content = re.sub(r"\n{3,}", "\n\n", content)
    return re.sub(r"  +", " ", content).strip()


def is_valid_response(content: str, agent_name: str) -> bool:
    """检查响应是否有效（不是超时或错误）"""
    return not (
        content.startswith(f"[{agent_name} 回应超时") or content.startswith("[错误:")
    )


def clean_response(content: str) -> str:
    """清理回复内容，移除 thinking 标签"""
    if not content:
        return content
    content = strip_thinking(content)
    return normalize_whitespace(content)


def role_to_speaker(role: str) -> str:
    """将消息 role 映射到显示名称，用于历史摘要。"""
    if role == "player":
        return "玩家"
    if role == "narrator":
        return "旁白"
    return role


def extract_identity(soul_content: str) -> str:
    """从 soul.md 的 <identity> 块提取一行公开身份标签；缺失或为空返回空字符串。"""
    if not soul_content:
        return ""
    match = _IDENTITY_PATTERN.search(soul_content)
    if not match:
        return ""
    text = match.group(1).strip()
    return " ".join(text.split())


def get_display_name(agent_name: str, soul_content: str) -> str:
    """从 soul.md 内容提取中文显示名，回退到 agent_name。"""
    role_match = re.search(r"<role>\s*([^\n<]+)", soul_content)
    if role_match:
        name_match = re.match(r"([\u4e00-\u9fff·]+)", role_match.group(1).strip())
        if name_match:
            return name_match.group(1)
    title_match = re.search(r"^#\s+(.+)$", soul_content, re.MULTILINE)
    if title_match:
        return title_match.group(1).strip()
    return agent_name


def normalize(content: str) -> str:
    """修复常见格式问题：字面 \\n、日期标题不规范、多余空行。"""
    content = content.replace("\\n", "\n")
    content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
    lines = content.split("\n")
    out = []
    for line in lines:
        stripped = line.strip()
        m = re.match(r"^(?:#{1,2}\s*|\*\*)?(\d{1,2}月\d{1,2}日)(?:\*\*)?(?:\s.*)?$", stripped)
        if m:
            out.append(f"## {m.group(1)}")
        elif re.match(r"^(?:\*\*(时间|地点|在场|关键词|重要度|内容)\*\*|(时间|地点|在场|关键词|重要度|内容))：", stripped):
            out.append(f"- {stripped}")
        else:
            out.append(line)
    content = "\n".join(out)
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()


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
