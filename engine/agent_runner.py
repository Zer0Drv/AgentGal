"""统一的 Agent 运行层。"""

import asyncio
from typing import TypeVar

from agents import RunConfig, Runner

from log_config.llm_usage import log_llm_usage
from log_config.routing import routing_logger

T = TypeVar("T")


def _build_run_config(workflow_name: str, trace_metadata: dict[str, str] | None = None) -> RunConfig:
    return RunConfig(
        tracing_disabled=False,
        workflow_name=workflow_name,
        trace_metadata=trace_metadata,
    )


def _log_run_usage(agent_name: str, phase: str, model_name: str, result) -> None:
    if not result.raw_responses:
        return
    usage = result.raw_responses[-1].usage
    cached = (
        usage.input_tokens_details.cached_tokens
        if usage.input_tokens_details and usage.input_tokens_details.cached_tokens
        else None
    )
    log_llm_usage(
        agent=agent_name,
        phase=phase,
        model=model_name,
        input_tokens=usage.input_tokens or None,
        output_tokens=usage.output_tokens or None,
        total_tokens=usage.total_tokens or None,
        cached_tokens=cached,
    )


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
        result = await asyncio.wait_for(
            Runner.run(
                agent,
                input=user_input,
                run_config=_build_run_config(workflow_name, trace_metadata),
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        routing_logger.error(f"{label} 运行超时（>{timeout_seconds}s），强制终止")
        raise

    _log_run_usage(usage_agent, usage_phase, model_name, result)
    return (result.final_output or "").strip()


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
        result = await asyncio.wait_for(
            Runner.run(
                agent,
                input=user_input,
                run_config=_build_run_config(workflow_name, trace_metadata),
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        routing_logger.error(
            f"{label} 运行超时（>{timeout_seconds}s），强制终止"
        )
        raise

    _log_run_usage(usage_agent, usage_phase, model_name, result)

    try:
        return result.final_output_as(output_type, raise_if_incorrect_type=True)
    except Exception as e:
        routing_logger.error(
            f"[{label}] structured output 解析失败: {e}，raw={result.final_output!r}"
        )
        raise
