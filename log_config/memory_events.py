"""Structured memory-domain log events."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from log_config.memory import memory_logger

_RETRIEVAL_RESULTS_EVENT = "agentgal.memory.retrieval.results"


def _float_attr(item: Mapping[str, Any], key: str) -> float:
    try:
        return float(item.get(key, 0.0))
    except (TypeError, ValueError):
        return 0.0


def _int_attr(item: Mapping[str, Any], key: str, default: int) -> int:
    try:
        return int(item.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _retrieval_result_items(ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "rank": index,
            "id": str(item.get("id", "")),
            "date": str(item.get("date", "")),
            "score": round(_float_attr(item, "score"), 4),
            "relevance": round(_float_attr(item, "relevance"), 4),
            "recency": round(_float_attr(item, "recency"), 4),
            "importance": _int_attr(item, "importance", 3),
            "content": str(item.get("content", "")).strip(),
        }
        for index, item in enumerate(ranked, start=1)
    ]


def log_retrieval_results(
    *,
    agent_name: str,
    query: str,
    ranked: list[dict[str, Any]],
    limit: int,
    hybrid_enabled: bool,
) -> None:
    """Log retrieval top hits as one structured event for Logfire."""
    results = _retrieval_result_items(ranked)
    result_ids = [item["id"] for item in results]
    memory_logger.info(
        "[Retrieval] Top结果: agent=%s, hits=%s, ids=%s",
        agent_name,
        len(results),
        ", ".join(result_ids) if result_ids else "无",
        extra={
            "event.name": _RETRIEVAL_RESULTS_EVENT,
            "retrieval.agent": agent_name,
            "retrieval.query": query,
            "retrieval.limit": limit,
            "retrieval.result_count": len(results),
            "retrieval.result_ids": result_ids,
            "retrieval.results": results,
            "retrieval.hybrid_enabled": hybrid_enabled,
        },
    )
