"""记忆检索查询构造与召回拼装。

检索 pipeline：embedding → vector candidates + BM25 candidates → hybrid fusion
→ (可选 rerank) → recency ranking → recall 状态更新
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import Any

from shared.config import (
    BM25_CANDIDATE_LIMIT,
    HYBRID_SEARCH_ENABLED,
    IMPORTANCE_WEIGHT,
    RELEVANCE_WEIGHT,
    RECENCY_WEIGHT,
    RECENCY_HALF_LIFE_DAYS,
    RECENCY_DATE_WEIGHT,
    RECENCY_RECALL_WEIGHT,
    VECTOR_RELEVANCE_WEIGHT,
    BM25_RELEVANCE_WEIGHT,
    VECTOR_CANDIDATE_LIMIT,
    RERANK_TOP_N,
    VECTOR_SEARCH_LIMIT,
    character_path,
)
from log_config.memory import memory_logger
from log_config.memory_events import log_retrieval_results
from llm.embedding import embed_sync
from llm.rerank import rerank, RERANK_MODEL
from memory.parser import extract_status_field, canonical_cn_date, game_day_diff
from storage.vector_store import vector_store, VectorStore, DB_PATH


# ----------------------------- Pipeline 工具函数 -----------------------------

_distance_to_relevance = lambda d: max(0.0, 1.0 - min(float(d), 2.0) / 2.0)

def _decay_from_game_date(current_game_date: str, past_game_date: str, half_life_days: float) -> float:
    """基于游戏内日期做指数衰减；无法解析时返回中性值。"""
    if half_life_days <= 0:
        return 0.5
    days_ago = game_day_diff(current_game_date, past_game_date)
    if days_ago is None:
        return 0.5
    return math.exp(-math.log(2) * days_ago / half_life_days)


def _recency_score(
    current_game_date: str | None,
    event_date: str,
    last_recalled_at: str,
) -> float:
    """计算单条记忆的 recency，按配置权重融合日期与 recall 两个信号。"""
    if not current_game_date:
        return 0.5
    event_score = _decay_from_game_date(current_game_date, event_date, RECENCY_HALF_LIFE_DAYS)
    recall_score = _decay_from_game_date(current_game_date, last_recalled_at or event_date, RECENCY_HALF_LIFE_DAYS)
    weighted_signals: list[tuple[float, float]] = []
    if RECENCY_DATE_WEIGHT > 0:
        weighted_signals.append((event_score, RECENCY_DATE_WEIGHT))
    if RECENCY_RECALL_WEIGHT > 0 and last_recalled_at:
        weighted_signals.append((recall_score, RECENCY_RECALL_WEIGHT))
    if not weighted_signals:
        return 0.5
    total_weight = sum(weight for _, weight in weighted_signals)
    if total_weight <= 0:
        return 0.5
    return sum(score * weight for score, weight in weighted_signals) / total_weight


def _importance_score(raw_importance: Any) -> float:
    """将 memory.md 的 1-5 重要度归一化到 [0, 1]。"""
    try:
        importance = int(raw_importance)
    except (TypeError, ValueError):
        return 0.5
    bounded = min(max(importance, 1), 5)
    return (bounded - 1) / 4.0


def _load_current_game_date() -> str | None:
    """从 narrator/status.md 读取当前游戏日。"""
    try:
        status = Path(character_path("narrator", "status.md")).read_text(encoding="utf-8")
    except OSError:
        return None
    current_time = extract_status_field(status, "当前时间") if status else ""
    return canonical_cn_date(current_time)


def hybrid_fusion(
    vec_rows: list[tuple],
    bm25_rows: list[tuple],
) -> list[dict[str, Any]]:
    """按 VECTOR_RELEVANCE_WEIGHT/BM25_RELEVANCE_WEIGHT 组合 relevance。"""
    docs: dict[int, dict[str, Any]] = {}

    for row in vec_rows:
        doc_id = int(row[0])
        docs[doc_id] = {
            "id": str(doc_id),
            "content": row[1],
            "vector_relevance": _distance_to_relevance(row[2]),
            "bm25_raw": None,
            "date": str(row[3] or ""),
            "last_recalled_at": str(row[4] or ""),
            "importance": int(row[5] or 3),
        }

    for row in bm25_rows:
        doc_id = int(row[0])
        entry = docs.setdefault(
            doc_id,
            {
                "id": str(doc_id),
                "content": row[1],
                "vector_relevance": 0.0,
                "bm25_raw": None,
                "date": str(row[3] or ""),
                "last_recalled_at": str(row[4] or ""),
                "importance": int(row[5] or 3),
            },
        )
        entry["content"] = row[1]
        entry["bm25_raw"] = abs(float(row[2]))
        if not entry["date"]:
            entry["date"] = str(row[3] or "")
        if not entry["last_recalled_at"]:
            entry["last_recalled_at"] = str(row[4] or "")
        entry["importance"] = int(row[5] or entry.get("importance", 3))

    bm25_values = [float(entry["bm25_raw"]) for entry in docs.values() if entry["bm25_raw"] is not None]
    bm25_min = min(bm25_values) if bm25_values else 0.0
    bm25_max = max(bm25_values) if bm25_values else 0.0

    results: list[dict[str, Any]] = []
    for entry in docs.values():
        bm25_raw = entry["bm25_raw"]
        if bm25_raw is None:
            bm25_relevance = 0.0
        elif bm25_max > bm25_min:
            bm25_relevance = (float(bm25_raw) - bm25_min) / (bm25_max - bm25_min)
        else:
            bm25_relevance = 1.0

        relevance = (
            VECTOR_RELEVANCE_WEIGHT * float(entry["vector_relevance"])
            + BM25_RELEVANCE_WEIGHT * bm25_relevance
        )
        results.append(
            {
                "id": entry["id"],
                "content": entry["content"],
                "relevance": relevance,
                "date": entry["date"],
                "last_recalled_at": entry["last_recalled_at"],
                "importance": int(entry.get("importance", 3) or 3),
            }
        )

    return sorted(results, key=lambda item: item["relevance"], reverse=True)


def apply_recency(
    candidates: list[dict[str, Any]],
    current_game_date: str | None,
) -> list[dict[str, Any]]:
    """叠加 recency / importance 信号，计算最终 score 并排序。"""
    total_weight = RELEVANCE_WEIGHT + RECENCY_WEIGHT + IMPORTANCE_WEIGHT
    relevance_weight = RELEVANCE_WEIGHT / total_weight if total_weight > 0 else 0.7
    recency_weight = RECENCY_WEIGHT / total_weight if total_weight > 0 else 0.3
    importance_weight = IMPORTANCE_WEIGHT / total_weight if total_weight > 0 else 0.0

    ranked: list[dict[str, Any]] = []
    for c in candidates:
        recency = _recency_score(
            current_game_date,
            str(c.get("date", "")),
            str(c.get("last_recalled_at", "")),
        )
        relevance = float(c.get("relevance", 0.0))
        importance = int(c.get("importance", 3) or 3)
        importance_score = _importance_score(importance)
        score = (
            relevance_weight * relevance
            + recency_weight * recency
            + importance_weight * importance_score
        )
        ranked.append(
            {
                "id": str(c["id"]),
                "content": str(c["content"]),
                "score": score,
                "relevance": relevance,
                "recency": recency,
                "importance": importance,
                "importance_score": importance_score,
                "date": str(c.get("date", "")),
                "last_recalled_at": str(c.get("last_recalled_at", "")),
            }
        )
    return sorted(ranked, key=lambda item: item["score"], reverse=True)


# ----------------------------- 主检索入口 -----------------------------

def _vec_rows_to_candidates(rows: list[tuple]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(row[0]),
            "content": row[1],
            "relevance": _distance_to_relevance(row[2]),
            "date": str(row[3] or ""),
            "last_recalled_at": str(row[4] or ""),
            "importance": int(row[5] or 3),
        }
        for row in rows
    ]


def search_memories(agent_name: str, query: str) -> str:
    """执行完整检索 pipeline 并格式化记忆块。

    pipeline 顺序：
    1. embed query → vector 候选（固定 VECTOR_CANDIDATE_LIMIT 条）
    2. hybrid: 若启用则叠加 BM25 候选，按 75/25 权重融合 relevance；
               否则直接用 vector distance 转换 relevance
    3. rerank（可选）: 用 rerank API 分替换 relevance，min-max 归一化到 [0,1]
    4. score: 叠加游戏内时间衰减与 memory.md 重要度，计算最终 score 并截取 VECTOR_SEARCH_LIMIT 条
    5. 更新命中条目的 last_recalled_at（DB）
    """
    if not query or not query.strip():
        return "（无相关记忆）"

    # Step 1: 计算查询向量
    try:
        qvec = embed_sync([query])[0]
    except Exception as e:
        memory_logger.error("[Retrieval] 查询嵌入失败: agent=%s, error=%s", agent_name, e)
        return "（无相关记忆）"

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(DB_PATH)
        VectorStore._load_sqlite_vec_sync(conn)

        # Step 2: 拉取候选，hybrid 时合并 BM25；否则纯向量
        vec_rows = vector_store.get_vector_candidates(conn, agent_name, qvec, VECTOR_CANDIDATE_LIMIT)

        if HYBRID_SEARCH_ENABLED:
            bm25_rows = vector_store.get_bm25_candidates(conn, agent_name, query, BM25_CANDIDATE_LIMIT)
            if bm25_rows:
                candidates = hybrid_fusion(vec_rows, bm25_rows)
                memory_logger.info(
                    "[Retrieval] hybrid fusion: agent=%s, vec=%s, bm25=%s, merged=%s",
                    agent_name, len(vec_rows), len(bm25_rows), len(candidates),
                )
            else:
                # BM25 无命中时回退到纯向量 relevance
                candidates = _vec_rows_to_candidates(vec_rows)
        else:
            candidates = _vec_rows_to_candidates(vec_rows)

        # Step 3: rerank（可选）— 替换 relevance 分，rerank 失败时降级保留 fusion 结果
        if RERANK_MODEL and candidates:
            try:
                candidates = rerank(query, candidates, top_n=RERANK_TOP_N)
                memory_logger.info(
                    f"[Retrieval] rerank 完成: agent={agent_name}, 候选={len(vec_rows)}, 返回={len(candidates)}"
                )
            except Exception as e:
                memory_logger.warning(f"[Retrieval] rerank 失败，降级为 fusion 结果: agent={agent_name}, error={e}")

        # Step 4: recency 加权排序，截取最终返回数
        current_game_date = _load_current_game_date()
        ranked = apply_recency(candidates, current_game_date)[:VECTOR_SEARCH_LIMIT]

        # Step 5: 更新命中条目的 last_recalled_at
        recalled_ids = [r["id"] for r in ranked if r["id"]]
        if recalled_ids and current_game_date:
            try:
                vector_store.update_recall_timestamps(conn, recalled_ids, current_game_date)
                conn.commit()
            except Exception as e:
                memory_logger.warning("[Retrieval] 更新 last_recalled_at 失败: %s", e)

        memory_logger.info(
            "[Retrieval] 搜索完成: agent=%s, limit=%s, 命中=%s, hybrid=%s",
            agent_name, VECTOR_SEARCH_LIMIT, len(ranked), HYBRID_SEARCH_ENABLED,
        )
        log_retrieval_results(
            agent_name=agent_name,
            query=query,
            ranked=ranked,
            limit=VECTOR_SEARCH_LIMIT,
            hybrid_enabled=HYBRID_SEARCH_ENABLED,
        )

    except Exception as e:
        memory_logger.error("[Retrieval] 检索失败: agent=%s, error=%s", agent_name, e)
        return "（无相关记忆）"
    finally:
        if conn is not None:
            conn.close()

    memories = [r["content"].strip() for r in ranked if r["content"].strip()]
    return "\n\n---\n\n".join(memories) if memories else "（无相关记忆）"
