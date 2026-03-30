"""消息路由系统 - 维护每个角色的独立对话历史"""

from shared.config import get_agent_names
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
        """统一的消息广播方法 — 只写入 narrator 的 jsonl（单一数据源）

        Args:
            targets: 原始目标角色列表
            message: 要广播的消息字典
        """
        # 确保 visible_to 包含 narrator（上帝视角）
        visible = targets.copy()
        if "narrator" not in visible:
            visible.append("narrator")

        visible = list(dict.fromkeys(visible))

        message["visible_to"] = visible
        await append_message(message)

    async def broadcast_player_message(self, targets: list[str], content: str):
        """
        广播玩家消息到所有 targets 的 jsonl

        Args:
            targets: 需要回应的角色列表
            content: 玩家消息内容
        """
        message = {
            "role": "player",
            "content": content,
            "visible_to": targets,
        }
        await self._broadcast_message(targets, message)

    async def broadcast_agent_response(
        self, agent_name: str, targets: list[str], content: str
    ):
        """
        广播角色回应到所有 targets（包括自己）的 jsonl

        Args:
            agent_name: 回应的角色名
            targets: 需要看到这条回应的角色列表（原消息的 targets）
            content: 回应内容
        """
        message = {
            "role": agent_name,
            "content": content,
            "visible_to": targets,
        }
        await self._broadcast_message(targets, message)




# 全局实例
message_router = MessageRouter()
