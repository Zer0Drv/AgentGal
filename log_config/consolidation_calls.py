"""整理器 LLM 调用日志 - 记录每步 prompt + 回包，方便排查合并问题"""

import logging
import os
from logging.handlers import RotatingFileHandler

LOGS_DIR = "logs/memory"
os.makedirs(LOGS_DIR, exist_ok=True)

_logger = logging.getLogger("consolidation_calls")
_logger.setLevel(logging.DEBUG)
_logger.propagate = False

if not _logger.handlers:
    _h = RotatingFileHandler(
        f"{LOGS_DIR}/consolidation_calls.log",
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8",
    )
    _h.setLevel(logging.DEBUG)
    _h.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_h)


def log_consolidation_call(
    agent_name: str,
    step: str,
    prompt: str,
    response: str,
    usage: dict | None = None,
) -> None:
    """记录整理器单次 LLM 调用（prompt + response）。

    Args:
        agent_name: 角色名
        step: 步骤名，如 "step1_merge" / "step2_growth" / "step3_dedup"
        prompt: 发送给 LLM 的完整 prompt
        response: LLM 返回的原始文本
        usage: token 用量 dict（可选）
    """
    sep = "=" * 80
    lines = [
        f"\n{sep}",
        f"[{agent_name}] {step}",
        sep,
        "[PROMPT]",
        prompt,
        "",
        "[RESPONSE]",
        response,
    ]
    if usage:
        parts = []
        if usage.get("prompt_tokens") is not None:
            parts.append(f"in: {usage['prompt_tokens']}")
        if usage.get("completion_tokens") is not None:
            parts.append(f"out: {usage['completion_tokens']}")
        if usage.get("total_tokens") is not None:
            parts.append(f"total: {usage['total_tokens']}")
        lines += ["-" * 40, "[tokens] " + " | ".join(parts)]
    lines.append(f"{sep}\n")
    _logger.info("\n".join(lines))
