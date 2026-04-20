"""测试 PydanticAI runner wrapper。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import BaseModel

project_root = Path(__file__).parent.parent
os.chdir(project_root)

try:
    import agents.runner as agent_runner_module
except ModuleNotFoundError as exc:
    pytest.skip(f"skip agent runner tests: missing dependency ({exc})", allow_module_level=True)


class _StructuredOutput(BaseModel):
    content: str


class _FakeResult:
    def __init__(self, output):
        self.output = output
        self.response = "raw-response"


class _FakeAgent:
    def __init__(self, result: _FakeResult):
        self._result = result
        self.calls: list[str] = []
        self.metadata_calls: list[dict[str, str]] = []

    async def run(self, user_input: str, *, metadata: dict[str, str] | None = None):
        self.calls.append(user_input)
        self.metadata_calls.append(metadata or {})
        return self._result


@pytest.mark.asyncio
async def test_run_text_agent_returns_stripped_text():
    agent = _FakeAgent(_FakeResult("  hello  "))
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
    assert agent.metadata_calls == [
        {
            "workflow_name": "wf",
            "usage_agent": "tester",
            "usage_phase": "agent_run",
            "model_name": "deepseek-chat",
            "agent_name": "tester",
        }
    ]


@pytest.mark.asyncio
async def test_run_structured_agent_returns_typed_output():
    expected = _StructuredOutput(content="ok")
    agent = _FakeAgent(_FakeResult(expected))
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
    assert agent.metadata_calls == [
        {
            "workflow_name": "wf",
            "usage_agent": "tester",
            "usage_phase": "agent_run",
            "model_name": "deepseek-chat",
        }
    ]


@pytest.mark.asyncio
async def test_run_structured_agent_raises_on_unexpected_output_type():
    agent = _FakeAgent(_FakeResult("not-json"))

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
