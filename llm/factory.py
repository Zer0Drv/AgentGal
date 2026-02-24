import importlib
import os
from typing import Dict, Any, Type, Union

from .base import BaseLLMClient

class LLMClientFactory:
    _REGISTRY = {
        "openai": "llm.llm_parser:OpenAICompatibleClient",
        "qwen": "llm.llm_parser:OpenAICompatibleClient",
        "deepseek": "llm.llm_parser:OpenAICompatibleClient",
    }

    @classmethod
    def _bootstrap_env_provider(cls) -> None:
        env_provider = (os.getenv("LLM_PROVIDER") or "").strip().lower()
        if env_provider and env_provider not in cls._REGISTRY:
            cls._REGISTRY[env_provider] = "llm.llm_parser:OpenAICompatibleClient"

    @classmethod
    def create_client(cls, config: Union[Dict[str, Any], Any]) -> BaseLLMClient:
        if hasattr(config, "model_dump"):
            conf_dict = config.model_dump()
        elif isinstance(config, dict):
            conf_dict = config.copy()
        else:
            raise ValueError(f"Invalid config type: {type(config)}")

        cls._bootstrap_env_provider()

        provider = (conf_dict.get("provider") or os.getenv("LLM_PROVIDER") or "openai").lower()
        client_class = cls._get_client_class(provider)

        init_kwargs = {
            "api_url": conf_dict.get("api_url") or os.getenv("LLM_API_URL"),
            "api_key": conf_dict.get("api_key") or os.getenv("LLM_API_KEY", ""),
            "model": conf_dict.get("model_name") or conf_dict.get("model") or os.getenv("LLM_MODEL_ID"),
            "timeout": conf_dict.get("timeout", 60.0),
            "max_retries": conf_dict.get("max_retries", 3),
            "temperature": conf_dict.get("temperature"),
            "max_tokens": conf_dict.get("max_tokens"),
        }
        for k in ["max_keepalive_connections", "max_connections", "keepalive_expiry"]:
            if k in conf_dict:
                init_kwargs[k] = conf_dict[k]

        used_keys = set(init_kwargs.keys()) | {"provider","api_url","model_name","headers","temperature","max_tokens"}
        runtime_defaults = {k: v for k, v in conf_dict.items() if k not in used_keys and v is not None}

        init_kwargs = {k: v for k, v in init_kwargs.items() if v is not None}
        if not init_kwargs.get("api_url"):
            raise ValueError(f"Provider [{provider}] missing api_url")

        client = client_class(**init_kwargs)
        setattr(client, "default_params", runtime_defaults)
        return client

    @classmethod
    def _get_client_class(cls, provider: str) -> Type[BaseLLMClient]:
        target = cls._REGISTRY.get(provider)
        if not target:
            target = cls._REGISTRY["openai"]
        if isinstance(target, type):
            return target
        if isinstance(target, str):
            module_path, class_name = target.split(":")
            module = importlib.import_module(module_path)
            client_class = getattr(module, class_name)
            cls._REGISTRY[provider] = client_class
            return client_class
        raise ValueError(f"Invalid registry entry for {provider}")

    @classmethod
    def register_client(cls, provider: str, client_class: Type[BaseLLMClient]):
        cls._REGISTRY[provider.lower()] = client_class

    @classmethod
    def list_supported_providers(cls):
        return list(cls._REGISTRY.keys())
