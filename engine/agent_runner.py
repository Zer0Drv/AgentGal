"""统一的 Agent 运行层。"""

from __future__ import annotations

import asyncio
from typing import Any, TypeVar

from log_config.routing import routing_logger

T = TypeVar("T")


def _build_run_metadata(
    workflow_name: str,
    usage_agent: str,
    usage_phase: str,
    model_name: str,
    trace_metadata: dict[str, str] | None,
) -> dict[str, str]:
    metadata = {
        "workflow_name": workflow_name,
        "usage_agent": usage_agent,
        "usage_phase": usage_phase,
        "model_name": model_name,
    }
    if trace_metadata:
        metadata.update(trace_metadata)
    return metadata


async def _run_agent(
    agent,
    user_input: str,
    metadata: dict[str, str],
    timeout_seconds: float,
    label: str,
) -> Any:
    try:
        return await asyncio.wait_for(
            agent.run(user_input, metadata=metadata),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        routing_logger.error(f"{label} 运行超时（>{timeout_seconds}s），强制终止")
        raise


async def run_text_agent(
    *,
    agent,
    user_input: str,
    timeout_seconds: float,
    workflow_name: str,
    trace_metadata: dict[str, str] | None,
    usage_agent: str,
    usage_phase: str,
    model_name: str,
    error_label: str | None = None,
) -> str:
    """执行文本 Agent，返回原始字符串输出。"""
    label = error_label or usage_agent
    result = await _run_agent(
        agent,
        user_input,
        _build_run_metadata(workflow_name, usage_agent, usage_phase, model_name, trace_metadata),
        timeout_seconds,
        label,
    )

    output = result.output
    if not isinstance(output, str):
        routing_logger.error(f"[{label}] 文本输出类型异常: {type(output)!r}")
        raise TypeError(f"{label} expected str output, got {type(output)!r}")
    return output.strip()


async def run_structured_agent(
    *,
    agent,
    user_input: str,
    output_type: type[T],
    timeout_seconds: float,
    workflow_name: str,
    trace_metadata: dict[str, str] | None,
    usage_agent: str,
    usage_phase: str,
    model_name: str,
    error_label: str | None = None,
) -> T:
    """执行结构化 Agent，并统一处理超时、用量日志和 typed parse。"""
    label = error_label or usage_agent
    result = await _run_agent(
        agent,
        user_input,
        _build_run_metadata(workflow_name, usage_agent, usage_phase, model_name, trace_metadata),
        timeout_seconds,
        label,
    )

    output = result.output
    if isinstance(output, output_type):
        return output

    routing_logger.error(
        f"[{label}] structured output 类型异常: expected={output_type!r}, got={type(output)!r}, raw={result.response!r}"
    )
    raise TypeError(f"{label} expected {output_type!r}, got {type(output)!r}")
