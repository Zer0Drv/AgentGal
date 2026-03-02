"""
OpenAI 兼容 LLM 客户端实现
支持 OpenAI / Qwen / DeepSeek 及其他兼容 OpenAI 格式的 API

基于 BaseLLMClient 模板方法模式实现
"""

import json
from dataclasses import dataclass
from typing import List, Dict, Optional, Any, Literal

from .base import BaseLLMClient

StreamEventType = Literal[
    "stream_start",
    "reasoning_delta",
    "text_delta",
    "agent_done",
    "stream_end",
    "error",
]

@dataclass(slots=True)
class StreamEvent:
    """
    LLM 流式输出统一事件结构（供 FastAPI SSE 层直接消费）
    """

    type: StreamEventType
    agent: str
    content: str = ""
    index: int = 0
    done: bool = False
    error: str = ""

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "type": self.type,
            "agent": self.agent,
            "index": self.index,
            "done": self.done,
        }
        if self.content:
            payload["content"] = self.content
        if self.error:
            payload["error"] = self.error
        return payload

    def to_sse_data(self) -> str:
        return json.dumps(self.to_payload(), ensure_ascii=False)

class FastAPISSEFormatter:
    """
    FastAPI SSE 事件格式化器。

    用法（框架）：
    1) chat service 创建 formatter(agent_name)
    2) LLM 每来一个 chunk，调用 make_delta_events()
    3) 将返回的 StreamEvent.to_sse_data() 交给 StreamingResponse
    """

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self._index = 0

    def start(self) -> StreamEvent:
        self._index += 1
        return StreamEvent(
            type="stream_start",
            agent=self.agent_name,
            index=self._index,
        )

    def text_delta(self, text: str) -> StreamEvent:
        self._index += 1
        return StreamEvent(
            type="text_delta",
            agent=self.agent_name,
            content=text,
            index=self._index,
        )

    def reasoning_delta(self, text: str) -> StreamEvent:
        self._index += 1
        return StreamEvent(
            type="reasoning_delta",
            agent=self.agent_name,
            content=text,
            index=self._index,
        )

    def agent_done(self) -> StreamEvent:
        self._index += 1
        return StreamEvent(
            type="agent_done",
            agent=self.agent_name,
            done=True,
            index=self._index,
        )

    def stream_end(self) -> StreamEvent:
        self._index += 1
        return StreamEvent(
            type="stream_end",
            agent=self.agent_name,
            done=True,
            index=self._index,
        )

    def error(self, message: str) -> StreamEvent:
        self._index += 1
        return StreamEvent(
            type="error",
            agent=self.agent_name,
            done=True,
            error=message,
            index=self._index,
        )

class OpenAICompatibleClient(BaseLLMClient):
    """
    OpenAI 兼容协议 LLM 客户端

    继承 BaseLLMClient，只关注协议细节：
    1. Header 构造
    2. Payload 构造 (支持 enable_thinking 等参数，供兼容 Qwen/DeepSeek 等模型)
    3. 流式/非流式响应解析
    """

    def __init__(
        self,
        api_url: str,
        api_key: str,
        model: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        **kwargs,
    ):
        """
        初始化通用 OpenAI 兼容客户端

        Args:
            api_url: API Base URL。
                     注意：如果传入完整路径 ".../chat/completions"，会自动去除后缀以适配基类。
            api_key: API Key
            model: 模型名称
            temperature: 温度参数 (从 config 传入，默认 0.1)
            max_tokens: 最大 token 数，None 表示不发送此字段（由 API 使用各自默认值）
            timeout: 请求超时时间
            max_retries: 最大重试次数
        """
        # 1. URL 修正逻辑
        # BaseLLMClient 会自动拼接 "/chat/completions"，因此这里需要确保 api_url 是 Base URL
        # 例如：https://.../v1/chat/completions -> https://.../v1
        if api_url.endswith("/chat/completions"):
            api_url = api_url.replace("/chat/completions", "")

        # 2. 保存实例级默认参数 (None 表示不传给 API，由各提供商自行决定上限)
        self._temperature = temperature if temperature is not None else 0.1
        self._max_tokens = max_tokens  # None = 不在 payload 中发送

        # 3. 初始化基类
        super().__init__(
            api_url=api_url,
            api_key=api_key,
            model=model,
            timeout=timeout,
            max_retries=max_retries,
        )

    # ==================== 实现抽象方法 ====================

    def _get_headers(self) -> Dict[str, str]:
        """
        构建 API 请求头
        """
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _build_payload(
        self, messages: List[Dict], stream: bool, **kwargs
    ) -> Dict[str, Any]:
        """
        构建请求 Body
        支持动态覆盖 temperature, max_tokens, 以及兼容模型常见的 enable_thinking
        """
        # 优先使用 kwargs 中的参数，否则使用实例默认值
        temperature = kwargs.get("temperature", self._temperature)
        max_tokens = kwargs.get("max_tokens", self._max_tokens)
        enable_thinking = kwargs.get("enable_thinking", True)

        # 兼容 Qwen/DeepSeek：通过在问题后添加 "/no_think" 来控制思考模式
        # 如果不需要思考过程，在最后一条用户消息后追加 "/no_think"
        processed_messages = []
        for msg in messages:
            processed_messages.append(msg.copy())

        if not enable_thinking:
            # 找到最后一条用户消息，在后面添加 "/no_think"
            for msg in reversed(processed_messages):
                if msg.get("role") == "user":
                    msg["content"] = msg.get("content", "") + "/no_think"
                    break

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": processed_messages,
            "stream": stream,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        # 传递标准可选参数
        for key in ["top_p", "frequency_penalty", "presence_penalty", "stop"]:
            if key in kwargs:
                payload[key] = kwargs[key]

        return payload

    def _parse_stream_chunk(self, chunk_data: str) -> Optional[Dict[str, Any]]:
        """
        解析单行 SSE 数据 (JSON 字符串)

        Args:
            chunk_data: 去除了 'data: ' 前缀的字符串
        """
        if not chunk_data or chunk_data.strip() == "[DONE]":
            return {"type": "done", "text": ""}

        try:
            data = json.loads(chunk_data)
        except json.JSONDecodeError:
            # 忽略非 JSON 数据 (如 "[DONE]" 在某些情况下可能未被基类过滤，或心跳包)
            return None

        choices = data.get("choices", [])
        if not choices:
            return None

        first_choice = choices[0]
        delta = first_choice.get("delta", {})
        finish_reason = first_choice.get("finish_reason")

        # 1. 优先提取思考过程 (Reasoning)
        # 不同 OpenAI 兼容服务字段可能不同，常见为 reasoning_content
        reasoning = delta.get("reasoning_content", "")
        if reasoning:
            return {
                "type": "reasoning",
                "text": reasoning,
                "finish_reason": finish_reason,
            }

        # 2. 提取标准内容 (Content)
        content = delta.get("content", "")
        if content:
            return {
                "type": "content",
                "text": content,
                "finish_reason": finish_reason,
            }

        if finish_reason:
            return {"type": "done", "text": "", "finish_reason": finish_reason}

        return None

    def make_sse_events_from_chunk(
        self,
        formatter: FastAPISSEFormatter,
        chunk_data: str,
    ) -> list[StreamEvent]:
        """
        将一个 LLM chunk 转换为 SSE 事件（框架方法）。
        FastAPI 层后续可以直接迭代这个返回值。
        """
        parsed = self._parse_stream_chunk(chunk_data)
        if not parsed:
            return []

        event_type = parsed.get("type")
        text = parsed.get("text", "")

        if event_type == "reasoning":
            return [formatter.reasoning_delta(text)]
        if event_type == "content":
            return [formatter.text_delta(text)]
        if event_type == "done":
            return [formatter.agent_done()]
        return []

    def _parse_response(self, response_data: Dict) -> Dict[str, Any]:
        """
        解析非流式完整响应
        """
        if not response_data.get("choices"):
            raise ValueError(f"Invalid response format: {response_data}")

        choice = response_data["choices"][0]
        message = choice.get("message", {})

        return {
            "content": message.get("content", ""),
            "reasoning_content": message.get(
                "reasoning_content", ""
            ),  # 即使为空也返回空字符串
            "usage": response_data.get("usage", {}),
        }

# 兼容旧命名，避免现有引用立刻断裂
QwenClient = OpenAICompatibleClient
