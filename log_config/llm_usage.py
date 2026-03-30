"""LLM 调用 token 日志（整理 & 角色调用通用）。

写入 JSONL，便于后续统计费用与缓存命中率。默认开启，可用
`LLM_USAGE_LOG_ENABLED=false` 关闭。
"""

import json
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
import logging

LOGS_DIR = "logs/llm_usage"
os.makedirs(LOGS_DIR, exist_ok=True)

LOG_ENABLED = os.getenv("LLM_USAGE_LOG_ENABLED", "true").lower() == "true"

_logger = logging.getLogger("llm_usage_jsonl")
_logger.setLevel(logging.INFO)
_logger.propagate = False

if not _logger.handlers:
    handler = RotatingFileHandler(
        f"{LOGS_DIR}/llm_usage.jsonl",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)


def log_llm_usage(
    agent: str,
    phase: str,
    model: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    cached_tokens: int | None = None,
) -> None:
    """记录任意 LLM 调用的 token 消耗。

    Args:
        agent: 角色名称 / 调用方标识
        phase: 阶段/用途，如 "agent_run"、"consolidation.step1_merge"
        model: 模型 ID
        input_tokens: 输入 token 数
        output_tokens: 输出 token 数
        total_tokens: 总 token 数
        cached_tokens: 命中缓存的 token 数（provider 未返回时为 None）
    """
    if not LOG_ENABLED:
        return

    cache_hit_ratio = (
        round(cached_tokens / input_tokens * 100, 2)
        if input_tokens and cached_tokens
        else None
    )

    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "agent": agent,
        "phase": phase,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "cache_hit_ratio": cache_hit_ratio,
    }

    try:
        _logger.info(json.dumps(entry, ensure_ascii=False))
    except Exception:
        pass
