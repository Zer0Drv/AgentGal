"""LLM 配置 - 使用 OpenAI-compatible API URL、模型 ID 和 API key。"""

import os
from collections.abc import Callable

from shared.config import AGENT_TEMPERATURE


_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"


def _get_required_env(key: str) -> str:
    """获取必需的环境变量，不存在则抛出错误"""
    value = _get_optional_env(key)
    if not value:
        raise ValueError(f"{key} not set in environment")
    return value


def _get_optional_env(key: str) -> str | None:
    value = os.getenv(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _normalize_api_url(api_url: str) -> str:
    """归一化 OpenAI 兼容 Base URL，避免重复拼接 chat/completions。"""
    normalized = api_url.rstrip("/")
    if normalized.endswith(_CHAT_COMPLETIONS_SUFFIX):
        normalized = normalized[: -len(_CHAT_COMPLETIONS_SUFFIX)]
    return normalized


def get_llm_config(
    model_id: str | None = None,
    api_key: str | None = None,
    api_url: str | None = None,
) -> dict:
    """返回 LLM 配置 dict，供 agent_factory 构建 OpenAI-compatible chat model 使用。

    参数优先级：传入参数 > 环境变量

    Returns:
        {
            "api_url": str,
            "api_key": str,
            "model_id": str,
            "temperature": float,
        }
    """
    model_id = _normalize_optional(model_id) or _get_required_env("LLM_MODEL_ID")
    api_key = _normalize_optional(api_key) or _get_required_env("LLM_API_KEY")
    api_url = _normalize_optional(api_url) or _get_required_env("LLM_API_URL")
    return {
        "api_url": _normalize_api_url(api_url),
        "api_key": api_key,
        "model_id": model_id,
        "temperature": AGENT_TEMPERATURE,
    }


def _read_scoped_overrides(env_prefix: str) -> dict[str, str | None]:
    return {
        "model_id": _get_optional_env(f"{env_prefix}_MODEL_ID"),
        "api_key": _get_optional_env(f"{env_prefix}_API_KEY"),
        "api_url": _get_optional_env(f"{env_prefix}_API_URL"),
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
        fallback_config: dict | None = None

        def fallback_value(key: str) -> str:
            nonlocal fallback_config
            if fallback_config is None:
                fallback_config = fallback_getter()
            return fallback_config[key]

        config = get_llm_config(
            model_id=overrides["model_id"] or fallback_value("model_id"),
            api_key=overrides["api_key"] or fallback_value("api_key"),
            api_url=overrides["api_url"] or fallback_value("api_url"),
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


def get_episode_closure_detector_llm_config(temperature: float | None = None) -> dict:
    """返回 episode 闭合检测器使用的 LLM 配置。

    优先使用 EPISODE_CLOSURE_DETECTOR_LLM_* 系列环境变量，未设置则复用主 LLM 配置。
    temperature 不为 None 时覆盖默认值。
    """
    return _make_scoped_llm_config(
        "EPISODE_CLOSURE_DETECTOR_LLM", get_llm_config, temperature=temperature
    )
