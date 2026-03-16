"""集中配置管理

静态应用常量从项目根目录的 config.toml 加载。
部署相关配置（密钥、模型选择）在 .env 中。

注意：get_agent_names() 每次调用都会扫描目录，
确保 reset_game() 和 load 命令后能立即反映新的角色列表。
"""

import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHARACTERS_DIR = PROJECT_ROOT / "data" / "characters"

with open(PROJECT_ROOT / "config.toml", "rb") as _f:
    _cfg = tomllib.load(_f)

# 记忆整理
CONSOLIDATION_INTERVAL: int = _cfg["memory"]["consolidation_interval"]
CONSOLIDATION_TEMPERATURE: float = _cfg["consolidation"]["temperature"]
CONSOLIDATION_MAX_TOKENS: int | None = _cfg["consolidation"]["max_tokens"] or None
GROWTH_DEDUP_THRESHOLD: int = _cfg["consolidation"]["growth_dedup_threshold"]

# 向量检索
VECTOR_SEARCH_LIMIT: int = _cfg["vector"]["search_limit"]
RERANK_CANDIDATE_MULTIPLIER: int = _cfg["vector"]["rerank_candidate_multiplier"]
TIME_DECAY_ALPHA: float = _cfg["vector"]["time_decay_alpha"]
TIME_DECAY_HALF_LIFE_DAYS: float = _cfg["vector"]["time_decay_half_life_days"]
HYBRID_SEARCH_ENABLED: bool = _cfg["vector"]["hybrid_search_enabled"]
RRF_K: int = _cfg["vector"]["rrf_k"]
BM25_CANDIDATE_LIMIT: int = _cfg["vector"]["bm25_candidate_limit"]

# Agent 运行
AGENT_RUN_TIMEOUT_SECONDS: int = _cfg["agent"]["run_timeout_seconds"]

# 文本后处理
MAX_ACTIONS: int = _cfg["text"]["max_actions"]
MAX_ELLIPSIS: int = _cfg["text"]["max_ellipsis"]

# 历史轮数（设计决策，不暴露给用户修改）
HISTORY_LIMIT = 20


def character_path(character_name: str, *subpaths: str) -> str:
    """构建角色数据路径

    Args:
        character_name: 角色名称
        *subpaths: 子路径组件

    Returns:
        完整路径字符串

    Example:
        >>> character_path("lilith", "memory.md")
        "/abs/path/to/data/characters/lilith/memory.md"
    """
    return str(CHARACTERS_DIR / character_name / Path(*subpaths))


def get_agent_names(include_narrator: bool = True) -> list[str]:
    """获取角色名称列表（每次动态扫描 data/characters/）

    Args:
        include_narrator: 是否包含 narrator，默认 True

    Returns:
        角色名称列表，按字母顺序排序
    """
    if not CHARACTERS_DIR.exists():
        return []
    agents = sorted(
        d.name
        for d in CHARACTERS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".") and not d.name.startswith("_")
    )
    if not include_narrator:
        agents = [name for name in agents if name != "narrator"]
    return agents
