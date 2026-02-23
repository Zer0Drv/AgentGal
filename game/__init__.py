"""游戏状态管理模块"""

from .save_manager import (
    agent_path,
    narrator_raw_dir,
    load_opening_text,
    load_recent_raw_messages,
    has_existing_save,
    reset_logs,
    reset_game,
    export_save_archive,
)

__all__ = [
    "agent_path",
    "narrator_raw_dir",
    "load_opening_text",
    "load_recent_raw_messages",
    "has_existing_save",
    "reset_logs",
    "reset_game",
    "export_save_archive",
]
