"""消息路由系统 - 维护每个角色的独立对话历史"""

import json
import os
from datetime import datetime

from shared.config import get_agent_names, character_path


class MessageRouter:
    """消息路由系统 - 维护每个角色的独立对话历史"""

    def __init__(self):
        self.agents = get_agent_names()

    def _get_raw_path(self, agent_name: str, date: str = None) -> str:
        """获取某角色的 raw 对话文件路径"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        return character_path(agent_name, "raw", f"{date}.jsonl")

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

        # 去重并保持顺序
        seen = set()
        visible = [t for t in visible if not (t in seen or seen.add(t))]

        message["visible_to"] = visible

        # 只写入 narrator 的 jsonl（角色通过 visible_to 过滤读取）
        raw_path = self._get_raw_path("narrator")
        os.makedirs(os.path.dirname(raw_path), exist_ok=True)

        with open(raw_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")

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
