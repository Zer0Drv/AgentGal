"""测试 LLM 配置解析。"""

import os
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
os.chdir(project_root)

try:
    import llm.config as llm_config_module
except ModuleNotFoundError as exc:
    pytest.skip(f"skip llm config tests: missing dependency ({exc})", allow_module_level=True)


_CONFIG_KEYS = {"api_url", "api_key", "model_id", "temperature"}


_LLM_ENV_KEYS = [
    "LLM_MODEL_ID",
    "LLM_API_KEY",
    "LLM_API_URL",
    "NARRATOR_LLM_MODEL_ID",
    "NARRATOR_LLM_API_KEY",
    "NARRATOR_LLM_API_URL",
    "CHOICES_LLM_MODEL_ID",
    "CHOICES_LLM_API_KEY",
    "CHOICES_LLM_API_URL",
    "CHARACTER_FACTORY_LLM_MODEL_ID",
    "CHARACTER_FACTORY_LLM_API_KEY",
    "CHARACTER_FACTORY_LLM_API_URL",
    "CONSOLIDATION_LLM_MODEL_ID",
    "CONSOLIDATION_LLM_API_KEY",
    "CONSOLIDATION_LLM_API_URL",
    "EPISODE_CLOSURE_DETECTOR_LLM_MODEL_ID",
    "EPISODE_CLOSURE_DETECTOR_LLM_API_KEY",
    "EPISODE_CLOSURE_DETECTOR_LLM_API_URL",
]


def _clear_llm_env(monkeypatch):
    for key in _LLM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_get_llm_config_normalizes_full_chat_completions_url(monkeypatch):
    _clear_llm_env(monkeypatch)

    config = llm_config_module.get_llm_config(
        model_id="gpt-test",
        api_key="main-key",
        api_url="https://example.com/v1/chat/completions",
    )

    assert set(config) == _CONFIG_KEYS
    assert config["model_id"] == "gpt-test"
    assert config["api_key"] == "main-key"
    assert config["api_url"] == "https://example.com/v1"


def test_get_llm_config_reads_model_key_and_url(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "main-key")
    monkeypatch.setenv("LLM_MODEL_ID", "custom-model")
    monkeypatch.setenv("LLM_API_URL", "https://custom.example/v1/chat/completions")

    config = llm_config_module.get_llm_config()

    assert set(config) == _CONFIG_KEYS
    assert config["model_id"] == "custom-model"
    assert config["api_key"] == "main-key"
    assert config["api_url"] == "https://custom.example/v1"


def test_get_choices_llm_config_falls_back_to_narrator_config(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("NARRATOR_LLM_MODEL_ID", "gpt-narrator")
    monkeypatch.setenv("NARRATOR_LLM_API_KEY", "narrator-key")
    monkeypatch.setenv("NARRATOR_LLM_API_URL", "https://narrator.example/v1/chat/completions")

    config = llm_config_module.get_choices_llm_config()

    assert set(config) == _CONFIG_KEYS
    assert config["model_id"] == "gpt-narrator"
    assert config["api_key"] == "narrator-key"
    assert config["api_url"] == "https://narrator.example/v1"


def test_scoped_config_can_override_all_fields_without_main_config(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("CHOICES_LLM_MODEL_ID", "choices-model")
    monkeypatch.setenv("CHOICES_LLM_API_KEY", "choices-key")
    monkeypatch.setenv("CHOICES_LLM_API_URL", "https://choices.example/v1/chat/completions")

    config = llm_config_module.get_choices_llm_config()

    assert set(config) == _CONFIG_KEYS
    assert config["model_id"] == "choices-model"
    assert config["api_key"] == "choices-key"
    assert config["api_url"] == "https://choices.example/v1"


def test_scoped_config_partially_overrides_fallback(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_ID", "deepseek-main")
    monkeypatch.setenv("LLM_API_KEY", "main-key")
    monkeypatch.setenv("LLM_API_URL", "https://main.example/v1")
    monkeypatch.setenv("CONSOLIDATION_LLM_API_URL", "https://scoped.example/v1")

    config = llm_config_module.get_consolidation_llm_config()

    assert set(config) == _CONFIG_KEYS
    assert config["model_id"] == "deepseek-main"
    assert config["api_key"] == "main-key"
    assert config["api_url"] == "https://scoped.example/v1"


def test_get_consolidation_llm_config_uses_factory_for_partial_override(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_ID", "gpt-main")
    monkeypatch.setenv("LLM_API_KEY", "main-key")
    monkeypatch.setenv("LLM_API_URL", "https://main.example/v1")
    monkeypatch.setenv(
        "CONSOLIDATION_LLM_API_URL",
        "https://consolidation.example/v1/chat/completions",
    )

    config = llm_config_module.get_consolidation_llm_config(temperature=0.42)

    assert set(config) == _CONFIG_KEYS
    assert config["model_id"] == "gpt-main"
    assert config["api_key"] == "main-key"
    assert config["api_url"] == "https://consolidation.example/v1"
    assert config["temperature"] == 0.42


def test_get_llm_config_requires_api_url(monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_ID", "gpt-main")
    monkeypatch.setenv("LLM_API_KEY", "main-key")

    with pytest.raises(ValueError, match="LLM_API_URL"):
        llm_config_module.get_llm_config()
