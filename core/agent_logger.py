"""Agent 调用日志记录 - 使用 agno 的 post_hook 机制

通过环境变量控制:
    AGENT_LOG_ENABLED=true  # 启用日志记录（默认关闭）
"""

import os
import json
from datetime import datetime
from typing import Any, Optional


# 创建 logs 目录
LOGS_DIR = "logs"
os.makedirs(LOGS_DIR, exist_ok=True)

# 是否启用日志（默认关闭，需设置 AGENT_LOG_ENABLED=true 开启）
LOG_ENABLED = os.getenv("AGENT_LOG_ENABLED", "false").lower() == "true"


def log_agent_run(run_output: Any, agent: Any, session: Optional[Any] = None):
    """
    agno post-hook 函数，记录每次 Agent 调用

    使用方法:
        agent = Agent(
            ...
            post_hooks=[log_agent_run],
        )
    """
    if not LOG_ENABLED:
        return

    # 提取信息
    agent_name = agent.name if hasattr(agent, 'name') else 'unknown'
    timestamp = datetime.now().isoformat()

    # 从 run_output 提取内容
    content = run_output.content if hasattr(run_output, 'content') else str(run_output)
    messages = run_output.messages if hasattr(run_output, 'messages') else []
    tools = run_output.tools if hasattr(run_output, 'tools') else []
    metrics = run_output.metrics if hasattr(run_output, 'metrics') else None
    model = run_output.model if hasattr(run_output, 'model') else None

    # 提取 user input
    user_input = ""
    if hasattr(run_output, 'input') and run_output.input:
        if hasattr(run_output.input, 'input_content'):
            user_input = run_output.input.input_content
        else:
            user_input = str(run_output.input)

    # 提取 system message 和 history messages
    system_message = None
    history_messages = []
    if messages:
        for msg in messages:
            if hasattr(msg, 'role'):
                msg_content = msg.content if hasattr(msg, 'content') else str(msg)
                if msg.role == 'system':
                    system_message = msg_content
                elif msg.role in ('user', 'assistant'):
                    history_messages.append({
                        "role": msg.role,
                        "content": msg_content
                    })

    # 构建日志条目
    log_entry = {
        "timestamp": timestamp,
        "agent": agent_name,
        "model": model,
        "system_message": system_message,
        "user_input": user_input,
        "history_messages": history_messages,
        "response": content,
        "tools_used": [
            {
                "name": t.tool_name if hasattr(t, 'tool_name') else str(t),
                "input": t.tool_input if hasattr(t, 'tool_input') else None,
                "output": t.tool_output if hasattr(t, 'tool_output') else None,
            }
            for t in (tools or [])
        ],
        "metrics": {
            "input_tokens": metrics.input_tokens if hasattr(metrics, 'input_tokens') else None,
            "output_tokens": metrics.output_tokens if hasattr(metrics, 'output_tokens') else None,
            "total_tokens": metrics.total_tokens if hasattr(metrics, 'total_tokens') else None,
        } if metrics else None,
    }

    # 写入 JSONL 格式（便于后续分析）
    jsonl_path = f"{LOGS_DIR}/agent_calls.jsonl"
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    # 同时写入可读的文本日志
    text_path = f"{LOGS_DIR}/agent_calls_readable.log"
    with open(text_path, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*80}\n")
        f.write(f"Agent: {agent_name}\n")
        f.write(f"Model: {model}\n")
        f.write(f"Time: {timestamp}\n")
        f.write(f"{'='*80}\n")
        if system_message:
            f.write(f"【System Prompt】\n{system_message}\n")
            f.write(f"{'-'*80}\n")
        if history_messages:
            f.write(f"【History】\n")
            for msg in history_messages:
                f.write(f"  {msg['role']}: {msg['content'][:200]}...\n")
            f.write(f"{'-'*80}\n")
        f.write(f"【User Input】\n{user_input}\n")
        f.write(f"{'-'*80}\n")
        f.write(f"【Response】\n{content}\n")
        if tools:
            f.write(f"{'-'*80}\n")
            f.write(f"【Tools Used】\n")
            for t in tools:
                tool_name = t.tool_name if hasattr(t, 'tool_name') else str(t)
                f.write(f"  - {tool_name}\n")
        if metrics:
            f.write(f"{'-'*80}\n")
            f.write(f"【Metrics】\n")
            if hasattr(metrics, 'input_tokens'):
                f.write(f"  Input tokens: {metrics.input_tokens}\n")
            if hasattr(metrics, 'output_tokens'):
                f.write(f"  Output tokens: {metrics.output_tokens}\n")
            if hasattr(metrics, 'total_tokens'):
                f.write(f"  Total tokens: {metrics.total_tokens}\n")
        f.write(f"{'='*80}\n\n")
