"""集中配置管理 - 动态读取 data/characters 目录

注意：角色列表在模块导入时（应用启动时）读取一次并缓存，
运行期间不再重新扫描目录。
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

    return sorted([
        d.name for d in agents_dir.iterdir()
        if d.is_dir() and not d.name.startswith('.') and not d.name.startswith('_')
    ])


# 应用启动时扫描一次，后续不再变更
AGENT_NAMES: list[str] = _scan_agent_names()
VALID_RESPONSE_AGENTS: list[str] = [name for name in AGENT_NAMES if name != "narrator"]


def get_agent_names() -> list[str]:
    """获取所有角色名称列表（应用启动时已预加载）"""
    return AGENT_NAMES


def get_valid_response_agents() -> list[str]:
    """获取可以回应用户的角色列表（排除 narrator）"""
    return VALID_RESPONSE_AGENTS


# 历史限制配置
HISTORY_LIMIT_NARRATOR = int(os.getenv("HISTORY_LIMIT_NARRATOR", "20"))
HISTORY_LIMIT_DEFAULT = int(os.getenv("HISTORY_LIMIT_DEFAULT", "10"))

# 超时配置
AGENT_RUN_TIMEOUT_SECONDS = int(os.getenv("AGENT_RUN_TIMEOUT_SECONDS", "20"))

# 后处理配置
MAX_ACTIONS = int(os.getenv("MAX_ACTIONS", "3"))
MAX_ELLIPSIS = int(os.getenv("MAX_ELLIPSIS", "3"))
