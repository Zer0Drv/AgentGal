"""文本处理工具函数"""

import re

from engine.config import MAX_ACTIONS, MAX_ELLIPSIS

# =============================================================================
# 常量
# =============================================================================

_THINKING_PATTERN = re.compile(
    r"<thinking>.*?</thinking>|<think>.*?</think>|<reasoning>.*?</reasoning>",
    re.DOTALL,
)

# prompt 结构标签残留（模型误将 prompt 的 XML 闭合标签当作输出的一部分）
_PROMPT_TAG_RESIDUE = re.compile(
    r"</(?:format|rules|fields|goal|soul|growth|example|output)>",
)


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


def _make_limiter(max_count: int):
    """创建一个限制替换次数的回调工厂"""
    counter = {"n": 0}

    def replacer(m):
        counter["n"] += 1
        return m.group(0) if counter["n"] <= max_count else ""

    return replacer


def clean_response(content: str) -> str:
    """清理回复内容，移除 thinking 标签"""
    if not content:
        return content
    content = strip_thinking(content)
    content = _PROMPT_TAG_RESIDUE.sub("", content)
    return normalize_whitespace(content)


def process_character_response(content: str) -> str:
    """处理角色回复：清理 thinking + 限制动作和省略号"""
    if not content:
        return content

    # 清理 thinking
    result = strip_thinking(content)

    # 限制动作描写（保留前 MAX_ACTIONS 个）
    result = re.sub(r"（[^）]*）", _make_limiter(MAX_ACTIONS), result)

    # 限制省略号（保留前 MAX_ELLIPSIS 个）
    result = re.sub(r"……|\.{2,}", _make_limiter(MAX_ELLIPSIS), result)

    # 清理空白
    return normalize_whitespace(result)
