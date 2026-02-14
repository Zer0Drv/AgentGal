"""LLM 配置 - 支持多提供商（OpenAI、DeepSeek、Anthropic、OpenRouter 等）"""

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agno.model import Model


SUPPORTED_PROVIDERS = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "deepseek": "DeepSeek",
    "openrouter": "OpenRouter",
}


def _get_required_env(key: str) -> str:
    """获取必需的环境变量，不存在则抛出错误"""
    value = os.getenv(key)
    if not value:
        raise ValueError(f"{key} not set in environment")
    return value


def _create_openai_compatible_model(
    model_id: str,
    api_key: str,
    api_url: str | None = None,
) -> "Model":
    """创建 OpenAI 兼容格式的模型实例

    支持：OpenAI、DeepSeek、OpenRouter、SiliconFlow 等任何 OpenAI 兼容 API
    """
    from agno.models.openai import OpenAIChat

    # agno 默认将 system 映射为 "developer"（OpenAI 新约定），
    # DeepSeek 等兼容 API 不支持，需要覆盖回 "system"
    fixed_role_map = dict(OpenAIChat.default_role_map, system="system")

    return OpenAIChat(
        id=model_id,
        api_key=api_key,
        base_url=api_url if api_url else None,
        role_map=fixed_role_map,
    )


def _create_anthropic_model(
    model_id: str,
    api_key: str,
) -> "Model":
    """创建 Anthropic Claude 模型实例"""
    try:
        from agno.models.anthropic import Claude

        return Claude(
            id=model_id,
            api_key=api_key,
        )
    except ImportError:
        raise ImportError(
            "使用 Anthropic 模型需要安装 anthropic 包: "
            "pip install anthropic"
        )


def get_model(
    provider: str | None = None,
    model_id: str | None = None,
    api_key: str | None = None,
    api_url: str | None = None,
) -> "Model":
    """获取配置的模型实例

    参数优先级：传入参数 > 环境变量 > 默认值

    Args:
        provider: 模型提供商 (openai, anthropic, deepseek, openrouter)
        model_id: 模型 ID
        api_key: API 密钥
        api_url: API 端点 URL（OpenAI 兼容格式需要）

    Returns:
        Model: agno 模型实例
    """
    # 从环境变量或参数获取配置
    provider = (provider or os.getenv("LLM_PROVIDER", "deepseek")).lower()
    model_id = model_id or os.getenv("LLM_MODEL_ID", "deepseek-chat")
    api_key = api_key or _get_required_env("LLM_API_KEY")
    api_url = api_url or os.getenv("LLM_API_URL")

    # 根据不同提供商创建对应模型
    if provider in ("openai", "deepseek", "openrouter"):
        # 设置默认 API URL（如果未指定）
        if not api_url:
            if provider == "deepseek":
                api_url = "https://api.deepseek.com"
            elif provider == "openrouter":
                api_url = "https://openrouter.ai/api/v1"
            # openai 不需要默认 base_url，agno 会使用官方 endpoint

        return _create_openai_compatible_model(model_id, api_key, api_url)

    elif provider == "anthropic":
        return _create_anthropic_model(model_id, api_key)

    else:
        # 未知提供商，尝试作为 OpenAI 兼容格式处理
        if api_url:
            return _create_openai_compatible_model(model_id, api_key, api_url)
        raise ValueError(
            f"Unsupported LLM provider: {provider}. "
            f"Supported: {', '.join(SUPPORTED_PROVIDERS.keys())}. "
            f"Or provide LLM_API_URL for OpenAI-compatible endpoints."
        )


def get_consolidation_model() -> "Model":
    """获取记忆整理器使用的模型

    优先使用 CONSOLIDATION_* 系列配置，如未设置则复用主 LLM 配置
    """
    provider = os.getenv("CONSOLIDATION_LLM_PROVIDER")
    model_id = os.getenv("CONSOLIDATION_LLM_MODEL_ID")
    api_key = os.getenv("CONSOLIDATION_LLM_API_KEY")
    api_url = os.getenv("CONSOLIDATION_LLM_API_URL")

    # 如果没有任何独立配置，直接使用主 LLM
    if not any([provider, model_id, api_key, api_url]):
        return get_model()

    # 部分配置时，缺失项使用主 LLM 的默认值
    provider = provider or os.getenv("LLM_PROVIDER", "deepseek")
    model_id = model_id or os.getenv("LLM_MODEL_ID", "deepseek-chat")
    api_key = api_key or os.getenv("LLM_API_KEY")
    api_url = api_url or os.getenv("LLM_API_URL")

    if not api_key:
        raise ValueError(
            "CONSOLIDATION_LLM_API_KEY or LLM_API_KEY must be set"
        )

    return get_model(
        provider=provider,
        model_id=model_id,
        api_key=api_key,
        api_url=api_url,
    )
