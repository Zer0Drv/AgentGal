"""Agent 调用日志记录 - 使用 agno 的 post_hook 机制。

始终记录：可读日志 + JSONL + token 用量（转写到 llm_usage）。
"""

import os
import json
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any, Optional

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


def log_agent_run(run_output: Any, agent: Any, session: Optional[Any] = None):
    """
    agno post-hook 函数，记录每次 Agent 调用

    使用方法:
        agent = Agent(
            ...
            post_hooks=[log_agent_run],
        )
    """
    # 提取信息
    agent_name = agent.name if hasattr(agent, "name") else "unknown"
    timestamp = datetime.now().isoformat()

    # 从 run_output 提取内容
    content = run_output.content if hasattr(run_output, "content") else str(run_output)
    messages = run_output.messages if hasattr(run_output, "messages") else []
    tools = run_output.tools if hasattr(run_output, "tools") else []
    metrics = run_output.metrics if hasattr(run_output, "metrics") else None
    model = run_output.model if hasattr(run_output, "model") else None

    # 提取 user input
    user_input = ""
    if hasattr(run_output, "input") and run_output.input:
        if hasattr(run_output.input, "input_content"):
            user_input = run_output.input.input_content
        else:
            user_input = str(run_output.input)

    # 提取完整的 messages 数组（发送给模型的完整上下文）
    full_messages = []
    if messages:
        for msg in messages:
            msg_dict = {
                "role": msg.role if hasattr(msg, "role") else "unknown",
                "content": msg.content if hasattr(msg, "content") else str(msg),
            }
            # 如果有 name 属性（如 tool 调用结果），也记录下来
            if hasattr(msg, "name") and msg.name:
                msg_dict["name"] = msg.name
            full_messages.append(msg_dict)

    # 构建日志条目
    log_entry = {
        "timestamp": timestamp,
        "agent": agent_name,
        "model": model,
        "request": full_messages
        if full_messages
        else [{"role": "user", "content": user_input}],
        "response": content,
        "tools_used": [
            {
                "name": t.tool_name if hasattr(t, "tool_name") else str(t),
                "input": t.tool_input if hasattr(t, "tool_input") else None,
                "output": t.tool_output if hasattr(t, "tool_output") else None,
            }
            for t in (tools or [])
        ],
        "metrics": {
            "input_tokens": metrics.input_tokens
            if hasattr(metrics, "input_tokens")
            else None,
            "output_tokens": metrics.output_tokens
            if hasattr(metrics, "output_tokens")
            else None,
            "total_tokens": metrics.total_tokens
            if hasattr(metrics, "total_tokens")
            else None,
        }
        if metrics
        else None,
    }

    # 写入 JSONL 格式（便于后续分析，自动轮转）
    _jsonl_logger.info(json.dumps(log_entry, ensure_ascii=False))

    # 写入可读的文本日志（自动轮转）
    lines = []
    lines.append("\n" + "=" * 80)
    lines.append(f"Agent: {agent_name} | Model: {model} | Time: {timestamp}")
    lines.append("=" * 80)
    # 直接显示完整的 message 流
    if full_messages:
        for msg in full_messages:
            role = msg.get("role", "unknown")
            content_text = msg.get("content", "")
            name = msg.get("name")
            if name:
                lines.append(f"[{role} | {name}]\n{content_text}\n")
            else:
                lines.append(f"[{role}]\n{content_text}\n")
    else:
        # 如果 Agno 没有返回 messages，显示 input
        lines.append(f"[input]\n{user_input}\n")
    if tools:
        lines.append("-" * 40)
        lines.append("[tools]")
        for t in tools:
            tool_name = t.tool_name if hasattr(t, "tool_name") else str(t)
            lines.append(f"  - {tool_name}")
    if metrics:
        lines.append("-" * 40)
        metrics_parts = []
        if hasattr(metrics, "input_tokens"):
            metrics_parts.append(f"in: {metrics.input_tokens}")
        if hasattr(metrics, "output_tokens"):
            metrics_parts.append(f"out: {metrics.output_tokens}")
        if hasattr(metrics, "total_tokens"):
            metrics_parts.append(f"total: {metrics.total_tokens}")
        lines.append("[metrics] " + " | ".join(metrics_parts))
    lines.append("=" * 80 + "\n")
    _text_logger.info("\n".join(lines))

    _log_usage_only(run_output, agent)


def _log_usage_only(run_output: Any, agent: Any):
    """将 token 消耗写入统一 LLM usage 日志。

    独立于 AGENT_LOG_ENABLED，避免漏记成本。
    """

    agent_name = agent.name if hasattr(agent, "name") else "unknown"
    metrics = run_output.metrics if hasattr(run_output, "metrics") else None
    model = run_output.model if hasattr(run_output, "model") else None
    usage_dict = None
    if metrics:
        usage_dict = {
            "prompt_tokens": getattr(metrics, "input_tokens", None),
            "completion_tokens": getattr(metrics, "output_tokens", None),
            "total_tokens": getattr(metrics, "total_tokens", None),
        }

    # user input / response 长度仅作参考
    user_input = ""
    if hasattr(run_output, "input") and run_output.input:
        if hasattr(run_output.input, "input_content"):
            user_input = run_output.input.input_content
        else:
            user_input = str(run_output.input)

    content = run_output.content if hasattr(run_output, "content") else str(run_output)

    log_llm_usage(
        agent_name,
        "agent_run",
        usage_dict,
        {
            "model": model,
            "user_input_len": len(user_input) if user_input else None,
            "response_len": len(content),
        },
    )
