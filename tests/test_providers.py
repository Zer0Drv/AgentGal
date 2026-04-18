"""测试 LLM provider 配置解析。"""

import os
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
os.chdir(project_root)

try:
    import llm.providers as providers_module
except ModuleNotFoundError as exc:
    pytest.skip(f"skip providers tests: missing dependency ({exc})", allow_module_level=True)


_LLM_ENV_KEYS = [
    "LLM_PROVIDER",
    "LLM_MODEL_ID",
    "LLM_API_KEY",
    "LLM_API_URL",
    "NARRATOR_LLM_PROVIDER",
    "NARRATOR_LLM_MODEL_ID",
    "NARRATOR_LLM_API_KEY",
    "NARRATOR_LLM_API_URL",
    "CHOICES_LLM_PROVIDER",
    "CHOICES_LLM_MODEL_ID",
    "CHOICES_LLM_API_KEY",
    "CHOICES_LLM_API_URL",
    "CONSOLIDATION_LLM_PROVIDER",
    "CONSOLIDATION_LLM_MODEL_ID",
    "CONSOLIDATION_LLM_API_KEY",
    "CONSOLIDATION_LLM_API_URL",
]


def _clear_llm_env(monkeypatch):
    for key in _LLM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_get_llm_config_normalizes_full_chat_completions_url(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "main-key")

    config = providers_module.get_llm_config(
        provider="openai",
        model_id="gpt-test",
        api_key="main-key",
        api_url="https://example.com/v1/chat/completions",
    )

    assert config["provider"] == "openai"
    assert config["api_url"] == "https://example.com/v1"


def test_get_choices_llm_config_falls_back_to_narrator_config(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "main-key")
    monkeypatch.setenv("NARRATOR_LLM_PROVIDER", "openai")
    monkeypatch.setenv("NARRATOR_LLM_MODEL_ID", "gpt-narrator")
    monkeypatch.setenv("NARRATOR_LLM_API_KEY", "narrator-key")
    monkeypatch.setenv("NARRATOR_LLM_API_URL", "https://narrator.example/v1/chat/completions")

    config = providers_module.get_choices_llm_config()

    assert config["provider"] == "openai"
    assert config["model"] == "gpt-narrator"
    assert config["api_key"] == "narrator-key"
    assert config["api_url"] == "https://narrator.example/v1"


def test_get_consolidation_llm_config_uses_factory_for_partial_override(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_MODEL_ID", "gpt-main")
    monkeypatch.setenv("LLM_API_KEY", "main-key")
    monkeypatch.setenv("LLM_API_URL", "https://main.example/v1")
    monkeypatch.setenv(
        "CONSOLIDATION_LLM_API_URL",
        "https://consolidation.example/v1/chat/completions",
    )

    config = providers_module.get_consolidation_llm_config(temperature=0.42)

    assert config["provider"] == "openai"
    assert config["model"] == "gpt-main"
    assert config["api_key"] == "main-key"
    assert config["api_url"] == "https://consolidation.example/v1"
    assert config["temperature"] == 0.42
