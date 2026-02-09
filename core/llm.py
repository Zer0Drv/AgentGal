"""LLM 配置 - 使用 agno 的模型系统"""

import os
from agno.models.deepseek import DeepSeek


def get_model():
    """
    获取 DeepSeek 配置的模型
    """
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY not set in environment")

    return DeepSeek(
        id=os.getenv("MODEL_ID", "deepseek-chat"),
        api_key=api_key,
    )
