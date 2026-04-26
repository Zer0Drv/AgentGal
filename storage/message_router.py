"""消息路由系统 - 维护每个角色的独立对话历史"""

from shared.config import get_agent_names
from storage.agent_files import read_turn_counter
from storage.history import append_message


class MessageRouter:
    """消息路由系统 - 维护每个角色的独立对话历史"""

    def __init__(self):
        self.agents = get_agent_names()

    async def _broadcast_message(
        self,
        targets: list[str],
        message: dict,
    ):
        """只写入 narrator 的 jsonl（单一数据源）。

        每条消息都带上当前全局 turn 号，供 `EpisodeClosureDetector` 与
        `consolidate_agent` 按 turn 切片。
        """
        visible = targets.copy()
        if "narrator" not in visible:
            visible.append("narrator")
        visible = list(dict.fromkeys(visible))

        message["visible_to"] = visible
        message["turn"] = read_turn_counter()
        await append_message(message)

    async def broadcast_player_message(
        self,
        targets: list[str],
        content: str,
    ):
        await self._broadcast_message(
            targets,
            {"role": "player", "content": content, "visible_to": targets},
        )

    async def broadcast_agent_response(
        self,
        agent_name: str,
        targets: list[str],
        content: str,
    ):
        await self._broadcast_message(
            targets,
            {"role": agent_name, "content": content, "visible_to": targets},
        )




# 全局实例
message_router = MessageRouter()
