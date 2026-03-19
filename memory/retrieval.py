"""记忆检索查询构造与召回拼装。

检索 pipeline：embedding → vector candidates + BM25 candidates → hybrid fusion
→ (可选 rerank) → recency ranking → recall 状态更新
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

import httpx

from engine.config import (
    BM25_CANDIDATE_LIMIT,
    HYBRID_SEARCH_ENABLED,
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
from memory.file_ops import extract_status_field, canonical_cn_date, game_day_diff
from memory.vector_store import vector_store, VectorStore, DB_PATH, _embed_sync


# ----------------------------- Rerank 配置 -----------------------------

_rerank_enabled = os.getenv("RERANK_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
RERANK_MODEL = os.getenv("RERANK_MODEL", "") if _rerank_enabled else ""
RERANK_API_KEY = os.getenv("RERANK_API_KEY") or os.getenv("EMBEDDING_API_KEY") or os.getenv("LLM_API_KEY", "")
RERANK_API_URL = os.getenv("RERANK_API_URL", "")


# ----------------------------- 场景查询构造 -----------------------------

def _build_retrieval_scene_summary(scene_summary: str) -> str:
    """将 narrator 场景摘要裁剪成适合检索的轻量结构信息。"""
    if not scene_summary or not scene_summary.strip():
        return ""

    time_line = ""
    location_line = ""
    present_lines: list[str] = []
    in_present_section = False

    for raw_line in scene_summary.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        field_match = re.match(r"^\*{0,2}(时间|地点|在场)\*{0,2}：\s*(.*)$", line)
        if field_match:
            field, value = field_match.groups()
            value = value.strip()
            if field == "时间":
                time_line = f"时间：{value}" if value else "时间："
            elif field == "地点":
                location_line = f"地点：{value}" if value else "地点："
            elif field == "在场":
                in_present_section = True
            continue

        if in_present_section and line.startswith("-"):
            present = line[1:].strip()
            if present and "不在场" not in present:
                present_lines.append(present)
            continue

        if in_present_section:
            in_present_section = False

    parts: list[str] = []
    if time_line:
        parts.append(time_line)
    if location_line:
        parts.append(location_line)
    if present_lines:
        parts.append("在场：")
        parts.extend(present_lines)
    return "\n".join(parts)


# ----------------------------- Pipeline 工具函数 -----------------------------

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
            "vector_relevance": max(0.0, 1.0 - min(float(row[2]), 2.0) / 2.0),
            "bm25_raw": None,
            "date": str(row[3] or ""),
            "last_recalled_at": str(row[4] or ""),
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
            },
        )
        entry["content"] = row[1]
        entry["bm25_raw"] = abs(float(row[2]))
        if not entry["date"]:
            entry["date"] = str(row[3] or "")
        if not entry["last_recalled_at"]:
            entry["last_recalled_at"] = str(row[4] or "")

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
            }
        )

    return sorted(results, key=lambda item: item["relevance"], reverse=True)


def rerank(
    query: str,
    candidates: list[dict[str, Any]],
    top_n: int,
) -> list[dict[str, Any]]:
    """调用 rerank API，用归一化后的 rerank 分替换 relevance。

    归一化：min-max 到 [0, 1]，与 BM25 处理方式一致。
    """
    documents = [c["content"] for c in candidates]
    resp = httpx.post(
        RERANK_API_URL,
        headers={"Authorization": f"Bearer {RERANK_API_KEY}", "Content-Type": "application/json"},
        json={"model": RERANK_MODEL, "query": query, "documents": documents, "top_n": top_n},
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json()["results"]

    scores = [r["relevance_score"] for r in results]
    min_s = min(scores)
    max_s = max(scores)
    normalized = [
        (r["index"], (r["relevance_score"] - min_s) / (max_s - min_s + 1e-9))
        for r in results
    ]
    # 按归一化分降序排列
    normalized.sort(key=lambda x: x[1], reverse=True)

    reranked: list[dict[str, Any]] = []
    for idx, norm_score in normalized:
        if idx >= len(candidates):
            continue
        entry = dict(candidates[idx])
        entry["relevance"] = norm_score
        reranked.append(entry)
    return reranked


def apply_recency(
    candidates: list[dict[str, Any]],
    current_game_date: str | None,
) -> list[dict[str, Any]]:
    """叠加 recency 信号，计算最终 score 并排序。"""
    total_weight = RELEVANCE_WEIGHT + RECENCY_WEIGHT
    relevance_weight = RELEVANCE_WEIGHT / total_weight if total_weight > 0 else 0.7
    recency_weight = RECENCY_WEIGHT / total_weight if total_weight > 0 else 0.3

    ranked: list[dict[str, Any]] = []
    for c in candidates:
        recency = _recency_score(
            current_game_date,
            str(c.get("date", "")),
            str(c.get("last_recalled_at", "")),
        )
        relevance = float(c.get("relevance", 0.0))
        score = relevance_weight * relevance + recency_weight * recency
        ranked.append(
            {
                "id": str(c["id"]),
                "content": str(c["content"]),
                "score": score,
                "relevance": relevance,
                "recency": recency,
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
            "relevance": max(0.0, 1.0 - min(float(row[2]), 2.0) / 2.0),
            "date": str(row[3] or ""),
            "last_recalled_at": str(row[4] or ""),
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
    4. recency: 叠加游戏内时间衰减，计算最终 score 并截取 VECTOR_SEARCH_LIMIT 条
    5. 更新命中条目的 last_recalled_at（DB + sidecar）
    """
    if not query or not query.strip():
        return "（无相关记忆）"

    # Step 1: 计算查询向量
    try:
        qvec = _embed_sync([query])[0]
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
        if ranked:
            summary = "; ".join(
                (
                    f"id={r.get('id')} date={r.get('date', '')} "
                    f"score={float(r.get('score', 0.0)):.3f} "
                    f"rel={float(r.get('relevance', 0.0)):.3f} "
                    f"rec={float(r.get('recency', 0.0)):.3f}"
                )
                for r in ranked
            )
            memory_logger.info("[Retrieval] Top结果: agent=%s, %s", agent_name, summary)
        else:
            memory_logger.info("[Retrieval] Top结果: agent=%s, （无命中）", agent_name)

    except Exception as e:
        memory_logger.error("[Retrieval] 检索失败: agent=%s, error=%s", agent_name, e)
        return "（无相关记忆）"
    finally:
        if conn is not None:
            conn.close()

    memories = [r["content"].strip() for r in ranked if r["content"].strip()]
    return "\n\n---\n\n".join(memories) if memories else "（无相关记忆）"


def build_memory_prefix(
    agent_name: str,
    user_input: str,
    scene_summary: str = "",
) -> str:
    """组装 `<relevant_memories>` 块。"""
    if agent_name == "narrator":
        return ""

    scene = _build_retrieval_scene_summary(scene_summary)
    query = "\n".join(part.strip() for part in [user_input, scene] if part and part.strip())
    memory_logger.info(
        "[Retrieval] agent=%s\nuser_input=%s\nquery=%s",
        agent_name,
        user_input.strip() or "（空）",
        query or "（空）",
    )
    relevant = search_memories(agent_name, query)
    return f"<relevant_memories>\n{relevant}\n</relevant_memories>"
