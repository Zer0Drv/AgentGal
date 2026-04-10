"""测试 PydanticAI runner wrapper。"""

from __future__ import annotations

import os
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest
from pydantic import BaseModel

project_root = Path(__file__).parent.parent
os.chdir(project_root)

try:
    import engine.agent_runner as agent_runner_module
except ModuleNotFoundError as exc:
    pytest.skip(f"skip agent runner tests: missing dependency ({exc})", allow_module_level=True)


class _StructuredOutput(BaseModel):
    content: str


class _FakeUsage:
    def __init__(self, *, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_tokens = cache_read_tokens
        self.total_tokens = input_tokens + output_tokens


class _FakeResult:
    def __init__(self, output, usage: _FakeUsage):
        self.output = output
        self._usage = usage
        self.response = "raw-response"

    def usage(self) -> _FakeUsage:
        return self._usage


class _FakeAgent:
    def __init__(self, result: _FakeResult):
        self._result = result
        self.calls: list[str] = []

    async def run(self, user_input: str):
        self.calls.append(user_input)
        return self._result


def _disable_logfire(monkeypatch):
    monkeypatch.setattr(agent_runner_module, "logfire_span", lambda *_args, **_kwargs: nullcontext())


class _FakeSpan:
    def __init__(self):
        self.attributes: dict = {}
        self.closed = False
        self.name: str | None = None

    def set_attributes(self, attributes: dict) -> None:
        if self.closed:
            raise RuntimeError("span already closed")
        self.attributes.update(attributes)


def _capture_logfire_span(monkeypatch) -> _FakeSpan:
    span = _FakeSpan()

    @contextmanager
    def _span_context(name, **_kwargs):
        span.name = name
        try:
            yield span
        finally:
            span.closed = True

    monkeypatch.setattr(agent_runner_module, "logfire_span", _span_context)
    return span


@pytest.mark.asyncio
async def test_run_text_agent_returns_stripped_text_and_logs_usage(monkeypatch):
    _disable_logfire(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(agent_runner_module, "log_llm_usage", lambda **kwargs: captured.update(kwargs))

    agent = _FakeAgent(_FakeResult("  hello  ", _FakeUsage(input_tokens=11, output_tokens=5, cache_read_tokens=3)))
    output = await agent_runner_module.run_text_agent(
        agent=agent,
        user_input="hi",
        timeout_seconds=1,
        workflow_name="wf",
        trace_metadata={"agent_name": "tester"},
        usage_agent="tester",
        usage_phase="agent_run",
        model_name="deepseek-chat",
    )

    assert output == "hello"
    assert agent.calls == ["hi"]
    assert captured["agent"] == "tester"
    assert captured["cached_tokens"] == 3


@pytest.mark.asyncio
async def test_run_structured_agent_returns_typed_output(monkeypatch):
    _disable_logfire(monkeypatch)
    monkeypatch.setattr(agent_runner_module, "log_llm_usage", lambda **_kwargs: None)

    expected = _StructuredOutput(content="ok")
    agent = _FakeAgent(_FakeResult(expected, _FakeUsage(input_tokens=7, output_tokens=2)))
    output = await agent_runner_module.run_structured_agent(
        agent=agent,
        user_input="hi",
        output_type=_StructuredOutput,
        timeout_seconds=1,
        workflow_name="wf",
        trace_metadata=None,
        usage_agent="tester",
        usage_phase="agent_run",
        model_name="deepseek-chat",
    )

    assert output == expected


@pytest.mark.asyncio
async def test_run_structured_agent_sets_usage_attributes_on_span(monkeypatch):
    span = _capture_logfire_span(monkeypatch)
    monkeypatch.setattr(agent_runner_module, "log_llm_usage", lambda **_kwargs: None)

    expected = _StructuredOutput(content="ok")
    agent = _FakeAgent(_FakeResult(expected, _FakeUsage(input_tokens=11, output_tokens=5, cache_read_tokens=3)))
    output = await agent_runner_module.run_structured_agent(
        agent=agent,
        user_input="hi",
        output_type=_StructuredOutput,
        timeout_seconds=1,
        workflow_name="wf",
        trace_metadata=None,
        usage_agent="tester",
        usage_phase="agent_run",
        model_name="deepseek-chat",
    )

    assert output == expected
    assert span.name == "tester.agent_run.structured"
    assert span.attributes["input_tokens"] == 11
    assert span.attributes["output_tokens"] == 5
    assert span.attributes["total_tokens"] == 16
    assert span.attributes["cache_read_tokens"] == 3
    assert span.attributes["token_hit_cache_ratio"] == pytest.approx(0.2727)
    assert span.attributes["token_hit_cache_percent"] == pytest.approx(27.27)
    assert span.closed is True


@pytest.mark.asyncio
async def test_run_text_agent_sets_zero_cache_ratio_on_span(monkeypatch):
    span = _capture_logfire_span(monkeypatch)
    monkeypatch.setattr(agent_runner_module, "log_llm_usage", lambda **_kwargs: None)

    agent = _FakeAgent(_FakeResult("ok", _FakeUsage(input_tokens=7, output_tokens=2, cache_read_tokens=0)))
    output = await agent_runner_module.run_text_agent(
        agent=agent,
        user_input="hi",
        timeout_seconds=1,
        workflow_name="wf",
        trace_metadata=None,
        usage_agent="tester",
        usage_phase="agent_run",
        model_name="deepseek-chat",
    )

    assert output == "ok"
    assert span.name == "tester.agent_run.text"
    assert span.attributes["cache_read_tokens"] == 0
    assert span.attributes["token_hit_cache_ratio"] == 0.0
    assert span.attributes["token_hit_cache_percent"] == 0.0
    assert span.closed is True


def test_build_trace_span_name_places_agent_first():
    assert (
        agent_runner_module._build_trace_span_name(
            usage_agent="narrator",
            usage_phase="agent_run",
            output_kind="structured",
        )
        == "narrator.agent_run.structured"
    )


@pytest.mark.asyncio
async def test_run_structured_agent_raises_on_unexpected_output_type(monkeypatch):
    _disable_logfire(monkeypatch)
    monkeypatch.setattr(agent_runner_module, "log_llm_usage", lambda **_kwargs: None)

    agent = _FakeAgent(_FakeResult("not-json", _FakeUsage(input_tokens=7, output_tokens=2)))

    with pytest.raises(TypeError):
        await agent_runner_module.run_structured_agent(
            agent=agent,
            user_input="hi",
            output_type=_StructuredOutput,
            timeout_seconds=1,
            workflow_name="wf",
            trace_metadata=None,
            usage_agent="tester",
            usage_phase="agent_run",
            model_name="deepseek-chat",
        )
