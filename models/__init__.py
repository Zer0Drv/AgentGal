"""领域模型层（最内层）：实体与值对象，零 I/O，依赖单向向内。

只依赖 shared 的纯函数（date_utils / text_utils），绝不 import shared.config
或任何外层（storage / agents / memory）。
"""

from models.character import Character
from models.memory import (
    EpisodeMemory,
    Understanding,
    UnderstandingHistoryEntry,
)

__all__ = [
    "Character",
    "EpisodeMemory",
    "Understanding",
    "UnderstandingHistoryEntry",
]
