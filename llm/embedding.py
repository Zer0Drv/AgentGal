"""OpenAI 兼容 Embeddings 客户端。

配置从环境变量读取，兼容 EMBEDDING_MODEL_ID / EMBEDDING_MODEL 两种变量名。
"""

from __future__ import annotations

import os

import httpx

from shared.config import EMBEDDING_REQUEST_TIMEOUT_SECONDS


EMBED_MODEL = os.getenv("EMBEDDING_MODEL_ID") or os.getenv("EMBEDDING_MODEL") or "text-embedding-3-small"
EMBED_API_KEY = os.getenv("EMBEDDING_API_KEY") or os.getenv("LLM_API_KEY", "")
EMBED_API_URL = os.getenv("EMBEDDING_API_URL") or os.getenv("LLM_API_URL", "")
EMBED_DIM = int(os.getenv("EMBEDDING_DIM", "1536"))


def _validate_embed_config() -> None:
    if not EMBED_API_KEY:
        raise ValueError("EMBEDDING_API_KEY 或 LLM_API_KEY 未配置，无法计算向量")
    if not EMBED_API_URL:
        raise ValueError("EMBEDDING_API_URL 或 LLM_API_URL 未配置，无法计算向量")


async def embed_async(texts: list[str]) -> list[list[float]]:
    """异步计算嵌入。"""
    _validate_embed_config()
    async with httpx.AsyncClient(timeout=EMBEDDING_REQUEST_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            EMBED_API_URL,
            headers={"Authorization": f"Bearer {EMBED_API_KEY}", "Content-Type": "application/json"},
            json={"model": EMBED_MODEL, "input": texts},
        )
        resp.raise_for_status()
        return [d["embedding"] for d in resp.json()["data"]]


def embed_sync(texts: list[str]) -> list[list[float]]:
    """同步计算嵌入（用于同步检索路径）。"""
    _validate_embed_config()
    resp = httpx.post(
        EMBED_API_URL,
        headers={"Authorization": f"Bearer {EMBED_API_KEY}", "Content-Type": "application/json"},
        json={"model": EMBED_MODEL, "input": texts},
        timeout=EMBEDDING_REQUEST_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return [d["embedding"] for d in resp.json()["data"]]
