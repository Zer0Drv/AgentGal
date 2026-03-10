"""游戏状态管理模块"""

from .save_manager import (
    narrator_raw_dir,
    load_prompt_file,
    load_conversation_history,
    has_existing_save,
    reset_logs,
    reset_game,
    export_save_archive,
)

__all__ = [
    "narrator_raw_dir",
    "load_prompt_file",
    "load_conversation_history",
    "has_existing_save",
    "reset_logs",
    "reset_game",
    "export_save_archive",
]
