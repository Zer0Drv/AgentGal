"""Tests for dialogue orchestration helpers."""

from types import SimpleNamespace

import pytest

import engine.conversation_flow as conversation_flow_module
from shared.config import AGENT_RUN_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_generate_choices_passes_required_timeout(monkeypatch):
    captured: dict = {}

    async def fake_run_structured_agent(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=["继续问下去"])

    monkeypatch.setattr(conversation_flow_module, "load_conversation_history", lambda turns: [])
    monkeypatch.setattr(
        conversation_flow_module,
        "build_history_transcript",
        lambda _agent_name, _messages: ("", None),
    )
    monkeypatch.setattr(
        conversation_flow_module,
        "get_choices_llm_config",
        lambda: {"model_id": "choices-model"},
    )
    monkeypatch.setattr(conversation_flow_module, "get_choices_agent", lambda: object())
    monkeypatch.setattr(
        conversation_flow_module,
        "run_structured_agent",
        fake_run_structured_agent,
    )

    choices = await conversation_flow_module.generate_choices("场景", [("alice", "回应")])

    assert choices == ["继续问下去"]
    assert captured["timeout_seconds"] == AGENT_RUN_TIMEOUT_SECONDS

