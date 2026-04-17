"""LLM 配置 - 支持多提供商（OpenAI、DeepSeek、OpenRouter 等 OpenAI 兼容 API）"""

import os
from collections.abc import Callable

from shared.config import AGENT_TEMPERATURE


SUPPORTED_PROVIDERS = ("openai", "deepseek", "openrouter")
_DEFAULT_PROVIDER = "deepseek"
_DEFAULT_MODEL_ID = "deepseek-chat"
_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"

# OpenAI 官方 API URL
_OPENAI_API_URL = "https://api.openai.com/v1"
_DEEPSEEK_API_URL = "https://api.deepseek.com/v1"
_OPENROUTER_API_URL = "https://openrouter.ai/api/v1"
_PROVIDER_DEFAULT_API_URLS = {
    "openai": _OPENAI_API_URL,
    "deepseek": _DEEPSEEK_API_URL,
    "openrouter": _OPENROUTER_API_URL,
}


def _get_required_env(key: str) -> str:
    """获取必需的环境变量，不存在则抛出错误"""
    value = os.getenv(key)
    if not value:
        raise ValueError(f"{key} not set in environment")
    return value


def _get_default_provider() -> str:
    return os.getenv("LLM_PROVIDER", _DEFAULT_PROVIDER).lower()


def _get_default_model_id() -> str:
    return os.getenv("LLM_MODEL_ID", _DEFAULT_MODEL_ID)


def _normalize_api_url(api_url: str) -> str:
    """归一化 OpenAI 兼容 Base URL，避免重复拼接 chat/completions。"""
    normalized = api_url.rstrip("/")
    if normalized.endswith(_CHAT_COMPLETIONS_SUFFIX):
        normalized = normalized[: -len(_CHAT_COMPLETIONS_SUFFIX)]
    return normalized


def _resolve_api_url(provider: str, api_url: str | None) -> str:
    """根据 provider 决定最终 API URL。"""
    if api_url:
        return _normalize_api_url(api_url)
    default_api_url = _PROVIDER_DEFAULT_API_URLS.get(provider)
    if default_api_url:
        return default_api_url
    # 未知 provider 但没有 api_url
    raise ValueError(
        f"Unsupported LLM provider: '{provider}'. "
        f"Supported: {', '.join(SUPPORTED_PROVIDERS)}. "
        "Or set LLM_API_URL for any OpenAI-compatible endpoint. "
        "To use Anthropic models, route them via OpenRouter (e.g. model: anthropic/claude-3-5-sonnet)."
    )


def get_llm_config(
    provider: str | None = None,
    model_id: str | None = None,
    api_key: str | None = None,
    api_url: str | None = None,
) -> dict:
    """返回 LLM 配置 dict，供 agent_factory 构建 OpenAI-compatible chat model 使用。

    参数优先级：传入参数 > 环境变量 > 默认值

    Returns:
        {"api_url": str, "api_key": str, "model": str, "temperature": float}
    """
    provider = (provider or _get_default_provider()).lower()
    model_id = model_id or _get_default_model_id()
    api_key = api_key or _get_required_env("LLM_API_KEY")
    api_url = _resolve_api_url(provider, api_url or os.getenv("LLM_API_URL"))
    return {"api_url": api_url, "api_key": api_key, "model": model_id, "temperature": AGENT_TEMPERATURE}


def _read_scoped_overrides(env_prefix: str) -> dict[str, str | None]:
    return {
        "provider": os.getenv(f"{env_prefix}_PROVIDER"),
        "model_id": os.getenv(f"{env_prefix}_MODEL_ID"),
        "api_key": os.getenv(f"{env_prefix}_API_KEY"),
        "api_url": os.getenv(f"{env_prefix}_API_URL"),
    }


def _make_scoped_llm_config(
    env_prefix: str,
    fallback_getter: Callable[[], dict],
    *,
    temperature: float | None = None,
) -> dict:
    """基于环境变量前缀构建 scoped LLM 配置。"""
    overrides = _read_scoped_overrides(env_prefix)
    if not any(overrides.values()):
        config = fallback_getter().copy()
    else:
        provider = (overrides["provider"] or _get_default_provider()).lower()
        model_id = overrides["model_id"] or _get_default_model_id()
        api_key = overrides["api_key"] or os.getenv("LLM_API_KEY")
        api_url = overrides["api_url"] or os.getenv("LLM_API_URL")

        if not api_key:
            raise ValueError(f"{env_prefix}_API_KEY or LLM_API_KEY must be set")

        config = get_llm_config(
            provider=provider,
            model_id=model_id,
            api_key=api_key,
            api_url=api_url,
        )

    if temperature is not None:
        config["temperature"] = temperature
    return config


def get_narrator_llm_config() -> dict:
    """返回 narrator 使用的 LLM 配置。

    优先使用 NARRATOR_LLM_* 系列环境变量，未设置则复用主 LLM 配置。
    """
    return _make_scoped_llm_config("NARRATOR_LLM", get_llm_config)


def get_choices_llm_config() -> dict:
    """返回选项生成使用的 LLM 配置。

    优先使用 CHOICES_LLM_* 系列环境变量，未设置则复用 narrator LLM 配置。
    """
    return _make_scoped_llm_config("CHOICES_LLM", get_narrator_llm_config)


def get_character_factory_llm_config() -> dict:
    """返回动态生成角色使用的 LLM 配置。

    优先使用 CHARACTER_FACTORY_LLM_* 系列环境变量，未设置则复用 narrator LLM 配置。
    """
    return _make_scoped_llm_config("CHARACTER_FACTORY_LLM", get_narrator_llm_config)


def get_consolidation_llm_config(temperature: float | None = None) -> dict:
    """返回记忆整理器使用的 LLM 配置。

    优先使用 CONSOLIDATION_LLM_* 系列环境变量，未设置则复用主 LLM 配置。
    temperature 不为 None 时覆盖默认值。
    """
    return _make_scoped_llm_config("CONSOLIDATION_LLM", get_llm_config, temperature=temperature)
