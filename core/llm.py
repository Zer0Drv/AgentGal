"""LLM 配置 - 使用 agno 的模型系统"""

import os
from agno.models.openai import OpenAIChat


def get_model():
    """
    获取 OpenRouter 配置的模型

    OpenRouter 兼容 OpenAI API 格式
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY not set in environment")

    return OpenAIChat(
        id=os.getenv("MODEL_ID", "anthropic/claude-3.5-sonnet"),
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
    )
