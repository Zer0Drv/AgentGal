import importlib
import os
from typing import Dict, Any, Type, Union
from loguru import logger

from .base import BaseLLMClient


class LLMClientFactory:
    """
    LLM 客户端工厂类 (优化版)
    特点：
    1. 支持懒加载 (Lazy Loading)，避免循环引用
    2. 深度集成 Pydantic Config
    3. 参数自动分流 (连接参数 vs 生成参数)
    """

    # 注册表：Provider -> "模块路径:类名"
    # 绝大多数现代模型都兼容 OpenAI 协议，所以使用各自的客户端
    _REGISTRY = {
        "openai": "llm.llm_parser:OpenAICompatibleClient",  # OpenAI 兼容协议
        "qwen": "llm.llm_parser:OpenAICompatibleClient",  # 通义千问
        "deepseek": "llm.llm_parser:OpenAICompatibleClient",  # DeepSeek (兼容 OpenAI)
        # 如果未来有特殊协议模型（非 OpenAI 接口），可以注册在这里
        # "zhipu": "llm.zhipu_client:ZhipuClient",
    }

    @classmethod
    def _bootstrap_env_provider(cls) -> None:
        """
        根据 .env 的 LLM_PROVIDER 自动补充 provider 映射。
        未显式注册的 provider 默认按 OpenAI 兼容协议处理。
        """
        env_provider = (os.getenv("LLM_PROVIDER") or "").strip().lower()
        if not env_provider:
            return
        if env_provider not in cls._REGISTRY:
            cls._REGISTRY[env_provider] = "llm.llm_parser:OpenAICompatibleClient"
            logger.info(
                f"Provider '{env_provider}' 从 .env 注入注册表，使用 OpenAICompatibleClient"
            )

    @classmethod
    def create_client(cls, config: Union[Dict[str, Any], Any]) -> BaseLLMClient:
        """
        创建 LLM 客户端实例

        Args:
            config: 可以是字典，也可以是 config.py 中的 Pydantic Settings 对象
                    (如 QwenSettings, GLMSettings)

        Returns:
            BaseLLMClient: 已初始化的客户端
        """
        # 1. 统一转为字典
        if hasattr(config, "model_dump"):
            conf_dict = config.model_dump()
        elif isinstance(config, dict):
            conf_dict = config.copy()
        else:
            raise ValueError(f"Invalid config type: {type(config)}")

        cls._bootstrap_env_provider()

        # 2. 获取 Provider 并加载对应的类（优先级：config > .env > 默认）
        provider = (
            conf_dict.get("provider") or os.getenv("LLM_PROVIDER") or "openai"
        ).lower()
        client_class = cls._get_client_class(provider)

        # 3. 拆分参数：哪些给 __init__ (连接用)，哪些给 default_params (生成用)

        # (A) 连接层参数 (对应 BaseLLMClient/__init__)
        # 注意映射关系：配置里的 api_url -> client 的 api_url
        init_kwargs = {
            "api_url": conf_dict.get("api_url") or os.getenv("LLM_API_URL"),
            "api_key": conf_dict.get("api_key") or os.getenv("LLM_API_KEY", ""),
            "model": (
                conf_dict.get("model_name")
                or conf_dict.get("model")
                or os.getenv("LLM_MODEL_ID")
            ),
            "timeout": conf_dict.get("timeout", 60.0),
            "max_retries": conf_dict.get("max_retries", 3),
            "temperature": conf_dict.get("temperature"),
            "max_tokens": conf_dict.get("max_tokens"),
        }

        # 注入连接池参数 (如果 config 里有)
        pool_keys = ["max_keepalive_connections", "max_connections", "keepalive_expiry"]
        for k in pool_keys:
            if k in conf_dict:
                init_kwargs[k] = conf_dict[k]

        # (B) 运行时生成参数 (对应 Chat/Stream 时的默认值)
        # 排除掉已经被提取的 keys (temperature 和 max_tokens 已传给 __init__)
        used_keys = (
            set(init_kwargs.keys())
            | set(pool_keys)
            | {
                "provider",
                "api_url",
                "model_name",
                "headers",
                "temperature",
                "max_tokens",
            }
        )
        runtime_defaults = {
            k: v for k, v in conf_dict.items() if k not in used_keys and v is not None
        }

        # 4. 实例化前清理：移除 None 值，让子类默认值生效
        init_kwargs = {k: v for k, v in init_kwargs.items() if v is not None}

        # 5. 实例化
        try:
            if not init_kwargs.get("api_url"):
                raise ValueError(f"Provider [{provider}] missing 'api_url'")

            # 创建实例
            client = client_class(**init_kwargs)

            # 挂载运行时默认参数 (temperature, max_tokens 等)
            # 这样在调用 client.stream_chat() 时如果没传参，就会用这里的默认值
            client.default_params = runtime_defaults

            logger.info(
                f"✓ Created Client: {provider} -> {client_class.__name__} "
                f"(Model: {init_kwargs.get('model', '')})"
            )
            return client

        except Exception as e:
            logger.error(f"Failed to create client for {provider}: {str(e)}")
            raise

    @classmethod
    def _get_client_class(cls, provider: str) -> Type[BaseLLMClient]:
        """
        内部方法：懒加载获取客户端类
        """
        # 1. 获取注册信息
        target = cls._REGISTRY.get(provider)
        if not target:
            logger.warning(
                f"Unknown provider '{provider}', falling back to default provider 'openai'"
            )
            target = cls._REGISTRY["openai"]

        # 2. 如果已经是类对象 (通过 register_client 注册的)
        if isinstance(target, type):
            return target

        # 3. 如果是字符串，动态导入 (Lazy Import)
        if isinstance(target, str):
            try:
                module_path, class_name = target.split(":")
                module = importlib.import_module(module_path)
                client_class = getattr(module, class_name)

                # 缓存一下，下次直接用类，不用再 import
                cls._REGISTRY[provider] = client_class
                return client_class
            except (ImportError, AttributeError) as e:
                raise ImportError(f"Could not load client class '{target}': {e}")

        raise ValueError(f"Invalid registry entry for {provider}")

    @classmethod
    def register_client(cls, provider: str, client_class: Type[BaseLLMClient]):
        """允许外部注册自定义客户端"""
        cls._REGISTRY[provider.lower()] = client_class
        logger.info(f"Registered custom client for: {provider}")

    @classmethod
    def list_supported_providers(cls):
        return list(cls._REGISTRY.keys())
