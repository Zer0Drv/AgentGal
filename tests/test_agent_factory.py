"""测试 PydanticAI Agent factory 的输出模式。"""

import os
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
os.chdir(project_root)

try:
    import engine.agent_factory as agent_factory_module
except ModuleNotFoundError as exc:
    pytest.skip(f"skip agent factory tests: missing dependency ({exc})", allow_module_level=True)


@pytest.fixture(autouse=True)
def reset_agent_caches():
    agent_factory_module._conversation_agents.clear()
    agent_factory_module._choices_agent = None
    agent_factory_module._state_updater_agent = None
    agent_factory_module._consolidation_agents.clear()
    yield
    agent_factory_module._conversation_agents.clear()
    agent_factory_module._choices_agent = None
    agent_factory_module._state_updater_agent = None
    agent_factory_module._consolidation_agents.clear()


def _fake_config():
    return {
        "provider": "deepseek",
        "api_url": "https://example.com/v1",
        "api_key": "test-key",
        "model": "deepseek-chat",
        "temperature": 0.2,
    }


def test_conversation_agents_use_prompted_output(monkeypatch):
    monkeypatch.setattr(agent_factory_module, "read_agent_file", lambda *_args: "# soul")
    monkeypatch.setattr(agent_factory_module, "build_system_prompt", lambda *_args: "system prompt")
    monkeypatch.setattr(agent_factory_module, "get_llm_config", _fake_config)
    monkeypatch.setattr(agent_factory_module, "get_narrator_llm_config", _fake_config)

    narrator = agent_factory_module.get_conversation_agent("narrator")
    character = agent_factory_module.get_conversation_agent("mitsuki")

    assert narrator._output_schema.mode == "prompted"
    assert character._output_schema.mode == "prompted"


def test_auxiliary_structured_agents_use_prompted_output(monkeypatch, tmp_path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("prompt", encoding="utf-8")

    monkeypatch.setattr(agent_factory_module, "get_choices_llm_config", _fake_config)
    monkeypatch.setattr(agent_factory_module, "get_narrator_llm_config", _fake_config)
    monkeypatch.setattr(
        agent_factory_module,
        "get_consolidation_llm_config",
        lambda temperature=None: {**_fake_config(), "temperature": temperature or 0.2},
    )
    monkeypatch.setattr(agent_factory_module, "_CHOICES_PROMPT_PATH", prompt_file)
    monkeypatch.setattr(agent_factory_module, "_STATE_UPDATER_PROMPT_PATH", prompt_file)
    monkeypatch.setattr(agent_factory_module, "load_text", lambda *_args: "prompt")

    choices = agent_factory_module.get_choices_agent()
    state_updater = agent_factory_module.get_state_updater_agent()
    memory_merge = agent_factory_module.get_memory_merge_agent()
    memory_metadata = agent_factory_module.get_memory_metadata_agent()
    growth_extract = agent_factory_module.get_growth_extract_agent()
    growth_dedup = agent_factory_module.get_growth_dedup_agent()

    assert choices._output_schema.mode == "prompted"
    assert state_updater._output_schema.mode == "prompted"
    assert memory_merge._output_schema.mode == "prompted"
    assert memory_metadata._output_schema.mode == "prompted"
    assert growth_extract._output_schema.mode == "prompted"
    assert growth_dedup._output_schema.mode == "prompted"


def test_player_profile_agent_remains_text_output(monkeypatch):
    monkeypatch.setattr(
        agent_factory_module,
        "get_consolidation_llm_config",
        lambda temperature=None: {**_fake_config(), "temperature": temperature or 0.2},
    )
    monkeypatch.setattr(agent_factory_module, "load_text", lambda *_args: "prompt")

    player_profile = agent_factory_module.get_player_profile_agent()

    assert player_profile._output_schema.mode == "text"


def test_make_sdk_model_uses_provider_specific_provider():
    model = agent_factory_module._make_sdk_model(
        {
            "provider": "openrouter",
            "api_url": "https://openrouter.ai/api/v1",
            "api_key": "test-key",
            "model": "moonshotai/kimi-k2.5",
            "temperature": 0.2,
        }
    )

    assert model._provider.name == "openrouter"
