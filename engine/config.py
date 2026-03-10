"""集中配置管理 - 动态读取 data/characters 目录

注意：get_agent_names() 和 get_valid_response_agents() 每次调用都会扫描目录，
确保 reset_game() 和 load 命令后能立即反映新的角色列表。
"""

import os
from pathlib import Path


# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 角色数据根目录（绝对路径，避免受当前工作目录影响）
CHARACTERS_DIR = PROJECT_ROOT / "data" / "characters"


def character_path(character_name: str, *subpaths: str) -> str:
    """构建角色数据路径

    Args:
        character_name: 角色名称
        *subpaths: 子路径组件

    Returns:
        完整路径字符串

    Example:
        >>> character_path("lilith", "memory.md")
        "data/characters/lilith/memory.md"
        >>> character_path("lilith", "memory", "Memory.md")
        "/abs/path/to/data/characters/lilith/memory/Memory.md"
    """
    return str(CHARACTERS_DIR / character_name / Path(*subpaths))


def _scan_agent_names() -> list[str]:
    """扫描 data/characters/ 目录获取所有角色名称"""
    agents_dir = CHARACTERS_DIR
    if not agents_dir.exists():
        return []

    return sorted(
        [
            d.name
            for d in agents_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".") and not d.name.startswith("_")
        ]
    )


def get_agent_names(include_narrator: bool = True) -> list[str]:
    """获取角色名称列表（每次动态扫描 data/characters/）

    Args:
        include_narrator: 是否包含 narrator，默认 True

    Returns:
        角色名称列表，按字母顺序排序
    """
    agents = _scan_agent_names()
    if not include_narrator:
        agents = [name for name in agents if name != "narrator"]
    return agents


# 历史限制配置
HISTORY_LIMIT_NARRATOR = int(os.getenv("HISTORY_LIMIT_NARRATOR", "20"))
HISTORY_LIMIT_DEFAULT = int(os.getenv("HISTORY_LIMIT_DEFAULT", "10"))

# 场景类型 → 历史轮数映射
SCENE_HISTORY_LIMITS: dict[str, int] = {
    "intimate": 12,  # 亲密场景需要完整上下文
    "social": 8,     # 社交场景中等
    "default": HISTORY_LIMIT_DEFAULT,
}


def get_history_limit_by_scene_type(scene_type: str) -> int:
    """根据场景类型返回对应的历史轮数限制"""
    return SCENE_HISTORY_LIMITS.get(scene_type, HISTORY_LIMIT_DEFAULT)

# 超时配置
AGENT_RUN_TIMEOUT_SECONDS = int(os.getenv("AGENT_RUN_TIMEOUT_SECONDS", "20"))

# 后处理配置
MAX_ACTIONS = int(os.getenv("MAX_ACTIONS", "3"))
MAX_ELLIPSIS = int(os.getenv("MAX_ELLIPSIS", "3"))
