"""统一的 Agent 运行层。"""

from __future__ import annotations

import asyncio
from typing import TypeVar

from log_config.logfire import logfire_span
from log_config.routing import routing_logger

T = TypeVar("T")


def _build_usage_trace_attributes(result) -> dict[str, int | float]:
    usage = result.usage()
    if not usage:
        return {}

    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens
    total_tokens = usage.total_tokens
    cache_read_tokens = usage.cache_read_tokens

    attributes: dict[str, int | float] = {}
    if input_tokens is not None:
        attributes["input_tokens"] = input_tokens
    if output_tokens is not None:
        attributes["output_tokens"] = output_tokens
    if total_tokens is not None:
        attributes["total_tokens"] = total_tokens
    if cache_read_tokens is not None:
        attributes["cache_read_tokens"] = cache_read_tokens

    if input_tokens and input_tokens > 0:
        cache_ratio = round((cache_read_tokens or 0) / input_tokens, 4)
        attributes["token_hit_cache_ratio"] = cache_ratio
        attributes["token_hit_cache_percent"] = round(cache_ratio * 100, 2)

    return attributes


def _attach_usage_trace_attributes(span, result) -> None:
    if span is None or not hasattr(span, "set_attributes"):
        return

    attributes = _build_usage_trace_attributes(result)
    if not attributes:
        return

    try:
        span.set_attributes(attributes)
    except Exception:  # noqa: BLE001
        return


def _build_trace_span_name(*, usage_agent: str, usage_phase: str, output_kind: str) -> str:
    return f"{usage_agent}.{usage_phase}.{output_kind}"


def _build_trace_attributes(
    workflow_name: str,
    usage_agent: str,
    usage_phase: str,
    model_name: str,
    trace_metadata: dict[str, str] | None,
) -> dict[str, str]:
    attributes = {
        "workflow_name": workflow_name,
        "usage_agent": usage_agent,
        "usage_phase": usage_phase,
        "model_name": model_name,
    }
    if trace_metadata:
        attributes.update(trace_metadata)
    return attributes


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
    try:
        with logfire_span(
            _build_trace_span_name(
                usage_agent=usage_agent,
                usage_phase=usage_phase,
                output_kind="text",
            ),
            **_build_trace_attributes(
                workflow_name, usage_agent, usage_phase, model_name, trace_metadata
            ),
        ) as span:
            result = await asyncio.wait_for(agent.run(user_input), timeout=timeout_seconds)
            _attach_usage_trace_attributes(span, result)
    except asyncio.TimeoutError:
        routing_logger.error(f"{label} 运行超时（>{timeout_seconds}s），强制终止")
        raise

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
    try:
        with logfire_span(
            _build_trace_span_name(
                usage_agent=usage_agent,
                usage_phase=usage_phase,
                output_kind="structured",
            ),
            **_build_trace_attributes(
                workflow_name, usage_agent, usage_phase, model_name, trace_metadata
            ),
        ) as span:
            result = await asyncio.wait_for(agent.run(user_input), timeout=timeout_seconds)
            _attach_usage_trace_attributes(span, result)
    except asyncio.TimeoutError:
        routing_logger.error(f"{label} 运行超时（>{timeout_seconds}s），强制终止")
        raise

    output = result.output
    if isinstance(output, output_type):
        return output

    routing_logger.error(
        f"[{label}] structured output 类型异常: expected={output_type!r}, got={type(output)!r}, raw={result.response!r}"
    )
    raise TypeError(f"{label} expected {output_type!r}, got {type(output)!r}")
