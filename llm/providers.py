"""LLM 配置 - 支持多提供商（OpenAI、DeepSeek、OpenRouter 等 OpenAI 兼容 API）"""

import os

from engine.config import AGENT_TEMPERATURE

SUPPORTED_PROVIDERS = ("openai", "deepseek", "openrouter")

# OpenAI 官方 API URL
_OPENAI_API_URL = "https://api.openai.com/v1"
_DEEPSEEK_API_URL = "https://api.deepseek.com/v1"
_OPENROUTER_API_URL = "https://openrouter.ai/api/v1"


def _get_required_env(key: str) -> str:
    """获取必需的环境变量，不存在则抛出错误"""
    value = os.getenv(key)
    if not value:
        raise ValueError(f"{key} not set in environment")
    return value


def _resolve_api_url(provider: str, api_url: str | None) -> str:
    """根据 provider 决定最终 API URL。"""
    if api_url:
        return api_url
    if provider == "deepseek":
        return _DEEPSEEK_API_URL
    if provider == "openrouter":
        return _OPENROUTER_API_URL
    if provider == "openai":
        return _OPENAI_API_URL
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
    """返回创建 OpenAICompatibleClient 所需的配置 dict。

    参数优先级：传入参数 > 环境变量 > 默认值

    Returns:
        {"api_url": str, "api_key": str, "model": str, "temperature": float}
    """
    provider = (provider or os.getenv("LLM_PROVIDER", "deepseek")).lower()
    model_id = model_id or os.getenv("LLM_MODEL_ID", "deepseek-chat")
    api_key = api_key or _get_required_env("LLM_API_KEY")
    api_url = _resolve_api_url(provider, api_url or os.getenv("LLM_API_URL"))
    return {"api_url": api_url, "api_key": api_key, "model": model_id, "temperature": AGENT_TEMPERATURE}


def get_narrator_llm_config() -> dict:
    """返回 narrator 使用的 LLM 配置。

    优先使用 NARRATOR_LLM_* 系列环境变量，未设置则复用主 LLM 配置。
    """
    provider = os.getenv("NARRATOR_LLM_PROVIDER")
    model_id = os.getenv("NARRATOR_LLM_MODEL_ID")
    api_key = os.getenv("NARRATOR_LLM_API_KEY")
    api_url = os.getenv("NARRATOR_LLM_API_URL")

    if not any([provider, model_id, api_key, api_url]):
        return get_llm_config()

    provider = provider or os.getenv("LLM_PROVIDER", "deepseek")
    model_id = model_id or os.getenv("LLM_MODEL_ID", "deepseek-chat")
    api_key = api_key or os.getenv("LLM_API_KEY")
    api_url = api_url or os.getenv("LLM_API_URL")

    if not api_key:
        raise ValueError("NARRATOR_LLM_API_KEY or LLM_API_KEY must be set")

    return get_llm_config(provider=provider, model_id=model_id, api_key=api_key, api_url=api_url)


def get_choices_llm_config() -> dict:
    """返回选项生成使用的 LLM 配置。

    优先使用 CHOICES_LLM_* 系列环境变量，未设置则复用 narrator LLM 配置。
    """
    provider = os.getenv("CHOICES_LLM_PROVIDER")
    model_id = os.getenv("CHOICES_LLM_MODEL_ID")
    api_key = os.getenv("CHOICES_LLM_API_KEY")
    api_url = os.getenv("CHOICES_LLM_API_URL")

    if not any([provider, model_id, api_key, api_url]):
        return get_narrator_llm_config()

    provider = provider or os.getenv("LLM_PROVIDER", "deepseek")
    model_id = model_id or os.getenv("LLM_MODEL_ID", "deepseek-chat")
    api_key = api_key or os.getenv("LLM_API_KEY")
    api_url = api_url or os.getenv("LLM_API_URL")

    if not api_key:
        raise ValueError("CHOICES_LLM_API_KEY or LLM_API_KEY must be set")

    return get_llm_config(provider=provider, model_id=model_id, api_key=api_key, api_url=api_url)


def get_consolidation_llm_config(temperature: float | None = None) -> dict:
    """返回记忆整理器使用的 LLM 配置。

    优先使用 CONSOLIDATION_* 系列环境变量，未设置则复用主 LLM 配置。
    temperature 不为 None 时覆盖默认值。
    """
    provider = os.getenv("CONSOLIDATION_LLM_PROVIDER")
    model_id = os.getenv("CONSOLIDATION_LLM_MODEL_ID")
    api_key = os.getenv("CONSOLIDATION_LLM_API_KEY")
    api_url = os.getenv("CONSOLIDATION_LLM_API_URL")

    # 没有任何独立配置，直接复用主 LLM
    if not any([provider, model_id, api_key, api_url]):
        config = get_llm_config()
    else:
        # 部分配置时，缺失项使用主 LLM 的默认值
        provider = provider or os.getenv("LLM_PROVIDER", "deepseek")
        model_id = model_id or os.getenv("LLM_MODEL_ID", "deepseek-chat")
        api_key = api_key or os.getenv("LLM_API_KEY")
        api_url = api_url or os.getenv("LLM_API_URL")

        if not api_key:
            raise ValueError("CONSOLIDATION_LLM_API_KEY or LLM_API_KEY must be set")

        config = get_llm_config(provider=provider, model_id=model_id, api_key=api_key, api_url=api_url)

    if temperature is not None:
        config["temperature"] = temperature
    return config
