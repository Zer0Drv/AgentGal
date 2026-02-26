"""LLM 调用 token 日志（整理 & 角色调用通用）。

写入 JSONL，便于后续统计费用。默认开启，可用
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


def log_llm_usage(agent: str, phase: str, usage: dict | None, meta: dict | None = None):
    """记录任意 LLM 调用的 token 消耗。

    Args:
        agent: 角色名称 / 调用方标识
        phase: 阶段/用途，如 "memory"、"user"、"agent_run"
        usage: LLM 返回的 usage/metrics 字段（可能为空）
        meta: 额外信息（日期范围、prompt/output 长度、模型等）
    """

    if not LOG_ENABLED:
        return

    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "agent": agent,
        "phase": phase,
        "usage": usage or {},
        "meta": meta or {},
    }

    try:
        _logger.info(json.dumps(entry, ensure_ascii=False))
    except Exception:
        # 忽略单次日志失败，避免影响主流程
        pass