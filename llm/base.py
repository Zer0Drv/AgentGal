import httpx
import asyncio
from abc import ABC, abstractmethod
from typing import List, Dict, AsyncGenerator, Any, Optional

class BaseLLMClient(ABC):
    """
    LLM 客户端基类 (Template Pattern)
    负责：生命周期管理、HTTP通信、重试逻辑
    不负责：具体的 payload 构造、具体的 response 解析
    """

    def __init__(
        self,
        api_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        max_retries: int = 2,
    ):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries

        # 延迟初始化 client
        self._client: Optional[httpx.AsyncClient] = None

    async def initialize(self):
        """初始化 HTTP 连接池"""
        if not self._client or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.api_url,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
                headers=self._get_headers(),  # 初始化时注入 Header
            )

    async def close(self):
        """关闭连接"""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self):
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    # ================= 必须实现的抽象方法 =================

    @abstractmethod
    def _get_headers(self) -> Dict[str, str]:
        """定义鉴权 Header"""
        pass

    @abstractmethod
    def _build_payload(
        self, messages: List[Dict], stream: bool, **kwargs
    ) -> Dict[str, Any]:
        """定义发送给大模型的 JSON Body"""
        pass

    @abstractmethod
    def _parse_stream_chunk(self, chunk_data: str) -> Optional[Dict[str, Any]]:
        """
        解析流式数据的一行
        Return 格式约定: {"type": "reasoning"|"content", "text": "..."}
        如果该行无效或是结束符，返回 None
        """
        pass

    @abstractmethod
    def _parse_response(self, response_data: Dict) -> Dict[str, Any]:
        """解析非流式完整响应"""
        pass

    # ================= 公共调用接口 =================

    async def stream_chat(
        self, messages: List[Dict], **kwargs
    ) -> AsyncGenerator[Dict[str, str], None]:
        """
        流式对话接口
        Yields: {"type": "reasoning"|"content", "text": "..."}
        """
        await self._ensure_client()
        payload = self._build_payload(messages, stream=True, **kwargs)

        for attempt in range(self.retries_safe):
            try:
                client = await self._get_client_snapshot()
                async with client.stream(
                    "POST", "/chat/completions", json=payload
                ) as response:
                    response.raise_for_status()

                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data: ") or line == "data: [DONE]":
                            continue

                        # 移除前缀
                        raw_data = line[6:]

                        try:
                            # 调用子类解析逻辑
                            parsed_item = self._parse_stream_chunk(raw_data)
                            if parsed_item:
                                yield parsed_item
                        except Exception:
                            # ignore bad chunk and continue
                            continue
                return  # 成功后退出重试循环

            except (httpx.HTTPError, RuntimeError, AttributeError) as e:
                await self._handle_http_error(e, attempt)

    async def chat(self, messages: List[Dict], **kwargs) -> Dict[str, Any]:
        """非流式对话接口"""
        await self._ensure_client()
        payload = self._build_payload(messages, stream=False, **kwargs)

        for attempt in range(self.retries_safe):
            try:
                client = await self._get_client_snapshot()
                response = await client.post("/chat/completions", json=payload)
                response.raise_for_status()
                return self._parse_response(response.json())
            except (httpx.HTTPError, RuntimeError, AttributeError) as e:
                await self._handle_http_error(e, attempt)

        raise RuntimeError("Max retries exceeded")

    # ================= 内部辅助 =================

    @property
    def retries_safe(self):
        return self.max_retries + 1

    async def _ensure_client(self):
        if not self._client or self._client.is_closed:
            await self.initialize()

    async def _get_client_snapshot(self) -> httpx.AsyncClient:
        """返回当前可用 client 快照，规避并发 close 造成的空引用。"""
        await self._ensure_client()
        client = self._client
        if client is None:
            # 极端并发下可能刚 initialize 又被 close，重试一次。
            await self.initialize()
            client = self._client
        if client is None:
            raise RuntimeError("HTTP client unavailable")
        return client

    async def _handle_http_error(self, e: Exception, attempt: int):
        if attempt < self.max_retries:
            await asyncio.sleep(2**attempt)
        else:
            raise e
