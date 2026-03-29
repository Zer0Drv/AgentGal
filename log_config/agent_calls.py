"""Agent 调用日志记录。

始终记录：可读日志 + JSONL + token 用量（转写到 llm_usage）。
"""

import os
import json
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler

from log_config.llm_usage import log_llm_usage


# 创建 logs/agent 目录
LOGS_DIR = "logs/agent"
os.makedirs(LOGS_DIR, exist_ok=True)

# ---- RotatingFileHandler 日志器 ----
_jsonl_logger = logging.getLogger("agent_calls_jsonl")
_jsonl_logger.setLevel(logging.INFO)
_jsonl_logger.propagate = False

_text_logger = logging.getLogger("agent_calls_text")
_text_logger.setLevel(logging.INFO)
_text_logger.propagate = False

if not _jsonl_logger.handlers:
    _jh = RotatingFileHandler(
        f"{LOGS_DIR}/agent_calls.jsonl",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    _jh.setLevel(logging.INFO)
    _jh.setFormatter(logging.Formatter("%(message)s"))
    _jsonl_logger.addHandler(_jh)

if not _text_logger.handlers:
    _th = RotatingFileHandler(
        f"{LOGS_DIR}/agent_calls_readable.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    _th.setLevel(logging.INFO)
    _th.setFormatter(logging.Formatter("%(message)s"))
    _text_logger.addHandler(_th)


def log_agent_call(
    agent_name: str,
    model: str,
    messages: list[dict],
    response: dict,
) -> None:
    """记录每次 Agent 调用（可读日志 + JSONL + token 用量）。

    Args:
        agent_name: 角色名称
        model: 模型名称
        messages: 发送给模型的完整消息列表（含 system prompt）
        response: OpenAICompatibleClient.chat() 的返回值
                  {"content": str, "reasoning_content": str, "usage": dict}
                  usage 字段: {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}
    """
    timestamp = datetime.now().isoformat()
    content = response.get("content") or ""
    usage = response.get("usage") or {}

    # DeepSeek: prompt_cache_hit_tokens（平铺）
    # OpenRouter: prompt_tokens_details.cached_tokens（嵌套）
    prompt_tokens_details = usage.get("prompt_tokens_details") or {}
    cache_hit_tokens = usage.get("prompt_cache_hit_tokens") or prompt_tokens_details.get("cached_tokens")

    prompt_tokens = usage.get("prompt_tokens")
    cache_hit_ratio = (
        f"{cache_hit_tokens / prompt_tokens * 100:.2f}%"
        if prompt_tokens and cache_hit_tokens is not None
        else None
    )

    # 构建日志条目
    log_entry = {
        "timestamp": timestamp,
        "agent": agent_name,
        "model": model,
        "metrics": {
            "input_tokens": prompt_tokens,
            "output_tokens": usage.get("completion_tokens"),
            "cache_hit_tokens": cache_hit_tokens,
            "cache_hit_ratio": cache_hit_ratio,
        },
        "request": messages,
        "response": content,
    }
    _jsonl_logger.info(json.dumps(log_entry, ensure_ascii=False))

    # 可读文本日志
    lines = [
        "\n" + "=" * 80,
        f"Agent: {agent_name} | Model: {model} | Time: {timestamp}",
        "=" * 80,
    ]
    for msg in messages:
        role = msg.get("role", "unknown")
        content_text = msg.get("content", "")
        lines.append(f"[{role}]\n{content_text}\n")
    lines.append(f"[assistant]\n{content}\n")
    if usage:
        lines.append("-" * 40)
        parts = []
        if usage.get("prompt_tokens") is not None:
            parts.append(f"in: {usage['prompt_tokens']}")
        if usage.get("completion_tokens") is not None:
            parts.append(f"out: {usage['completion_tokens']}")
        if usage.get("total_tokens") is not None:
            parts.append(f"total: {usage['total_tokens']}")
        if cache_hit_tokens is not None:
            parts.append(f"cache_hit: {cache_hit_tokens}")
        if usage.get("prompt_cache_miss_tokens") is not None:
            parts.append(f"cache_miss: {usage['prompt_cache_miss_tokens']}")
        lines.append("[metrics] " + " | ".join(parts))
    lines.append("=" * 80 + "\n")
    _text_logger.info("\n".join(lines))

    # token 用量写入统一日志
    usage_dict = dict(usage) if usage else None
    log_llm_usage(
        agent_name,
        "agent_run",
        usage_dict,
        {"model": model, "response_len": len(content)},
    )
