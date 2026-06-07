"""领域实体：游戏角色 Character。

极薄实体——只承载本存档生命周期内不变的数据：name + 只读 soul 文本。
可变 / 共享状态（status.md、memory）不进实体：它们回合中被多方改写、且人工可编辑，
持有快照会过期，统一经 CharacterRepository 按需读写。
"""

from dataclasses import dataclass

from shared.text_utils import get_display_name


@dataclass(frozen=True)
class Character:
    """游戏角色领域实体（不可变数据）。"""

    name: str
    soul: str  # soul.md 全文，只读 prompt 资源

    @property
    def display_name(self) -> str:
        """从 soul.md 提取中文显示名，回退到 name。"""
        return get_display_name(self.name, self.soul)
