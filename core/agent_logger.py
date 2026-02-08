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

    # 提取完整的 messages 数组（发送给模型的完整上下文）
    full_messages = []
    if messages:
        for msg in messages:
            msg_dict = {
                "role": msg.role if hasattr(msg, 'role') else 'unknown',
                "content": msg.content if hasattr(msg, 'content') else str(msg),
            }
            # 如果有 name 属性（如 tool 调用结果），也记录下来
            if hasattr(msg, 'name') and msg.name:
                msg_dict["name"] = msg.name
            full_messages.append(msg_dict)

    # 构建日志条目
    log_entry = {
        "timestamp": timestamp,
        "agent": agent_name,
        "model": model,
        "request": full_messages if full_messages else [{"role": "user", "content": user_input}],
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
        f.write(f"Agent: {agent_name} | Model: {model} | Time: {timestamp}\n")
        f.write(f"{'='*80}\n")
        # 直接显示完整的发送给模型的内容
        f.write(f"【Request - 发送给模型的完整内容】\n")
        if full_messages:
            for msg in full_messages:
                role = msg.get("role", "unknown")
                content_text = msg.get("content", "")
                name = msg.get("name")
                if name:
                    f.write(f"[{role} | {name}]\n{content_text}\n\n")
                else:
                    f.write(f"[{role}]\n{content_text}\n\n")
        else:
            # 如果 Agno 没有返回 messages，显示 input
            f.write(f"[input]\n{user_input}\n\n")
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
