"""Agent 运行器 - 初始化 Agent 并处理消息广播"""

import os
import json
from datetime import datetime
from typing import List, Dict, Any
from agno.agent import Agent
from agno.models.openai import OpenAIChat

from .llm import get_model
from .tools import create_tools_for_agent


class AgentManager:
    """管理所有角色的 Agent 实例"""

    def __init__(self):
        self.agents: Dict[str, Agent] = {}
        self._init_agents()

    def _init_agents(self):
        """初始化所有角色 Agent"""
        for agent_name in ["lilith", "ruri", "mitsuki", "narrator"]:
            self.agents[agent_name] = self._create_agent(agent_name)

    def _create_agent(self, agent_name: str) -> Agent:
        """创建单个 Agent"""
        # 加载角色设定
        soul_content = self._load_soul(agent_name)

        # 为该角色创建专属工具（已绑定 agent_name）
        tools = create_tools_for_agent(agent_name)

        return Agent(
            name=agent_name,
            model=get_model(),
            instructions=[
                f"你是 {agent_name}。",
                soul_content,
                "你有自己的记忆、目标和认知。",
                "使用工具自主管理记忆和推进目标，无需询问许可。",
                "以第一人称回应，保持角色性格的一致性。",
                "不要提及自己是 AI 或系统。",
            ],
            tools=tools,
            markdown=True,
        )

    def _load_soul(self, agent_name: str) -> str:
        """加载 soul.md"""
        soul_path = f"agents/{agent_name}/soul.md"
        if os.path.exists(soul_path):
            with open(soul_path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def _load_context(self, agent_name: str) -> str:
        """加载用户认知和任务"""
        context_parts = []

        # 加载 user.md
        user_path = f"agents/{agent_name}/user.md"
        if os.path.exists(user_path):
            with open(user_path, "r", encoding="utf-8") as f:
                content = f.read()
                context_parts.append(f"## 对玩家的认知\n\n{content}")

        # 加载 tasks.md
        tasks_path = f"agents/{agent_name}/tasks.md"
        if os.path.exists(tasks_path):
            with open(tasks_path, "r", encoding="utf-8") as f:
                content = f.read()
                context_parts.append(f"## 当前目标\n\n{content}")

        return "\n\n".join(context_parts)

    async def run_agent(self, agent_name: str, user_input: str) -> str:
        """
        运行单个 Agent 获取回应

        Args:
            agent_name: 角色名
            user_input: 用户输入（已包含历史上下文）

        Returns:
            Agent 的回应文本
        """
        agent = self.agents.get(agent_name)
        if not agent:
            return f"[错误: 未找到角色 {agent_name}]"

        # 加载额外上下文
        context = self._load_context(agent_name)

        # 构建完整输入
        full_input = f"""{context}

---

当前对话:

{user_input}"""

        # 运行 Agent
        response = await agent.arun(full_input)
        return response.content


# 全局实例
agent_manager = AgentManager()


class MessageBroadcaster:
    """消息广播系统 - 维护每个角色的独立对话历史"""

    def __init__(self):
        self.agents = ["lilith", "ruri", "mitsuki", "narrator"]

    def _get_raw_path(self, agent_name: str, date: str = None) -> str:
        """获取某角色的 raw 对话文件路径"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        return f"agents/{agent_name}/memory/raw/{date}.jsonl"

    def _ensure_dir(self, path: str):
        """确保目录存在"""
        os.makedirs(os.path.dirname(path), exist_ok=True)

    async def broadcast_player_message(
        self, targets: List[str], content: str
    ):
        """
        广播玩家消息到所有 targets 的 jsonl

        Args:
            targets: 需要回应的角色列表
            content: 玩家消息内容
        """
        timestamp = datetime.now().isoformat()
        message = {
            "timestamp": timestamp,
            "role": "player",
            "content": content,
            "visible_to": targets,
        }

        # narrator 作为 DM 需要看到所有消息（上帝视角）
        broadcast_targets = targets.copy()
        if "narrator" not in broadcast_targets:
            broadcast_targets.append("narrator")

        for agent_name in broadcast_targets:
            raw_path = self._get_raw_path(agent_name)
            self._ensure_dir(raw_path)

            with open(raw_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(message, ensure_ascii=False) + "\n")

    async def broadcast_agent_response(
        self, agent_name: str, targets: List[str], content: str
    ):
        """
        广播角色回应到所有 targets（包括自己）的 jsonl

        Args:
            agent_name: 回应的角色名
            targets: 需要看到这条回应的角色列表（原消息的 targets）
            content: 回应内容
        """
        timestamp = datetime.now().isoformat()
        message = {
            "timestamp": timestamp,
            "role": agent_name,
            "content": content,
            "visible_to": targets,
        }

        # 确定广播范围
        broadcast_targets = targets.copy()

        # narrator 的场景描述应该被所有角色看到
        if agent_name == "narrator":
            broadcast_targets = self.agents.copy()

        # narrator 作为 DM 需要看到所有角色的回应（上帝视角）
        if "narrator" not in broadcast_targets:
            broadcast_targets.append("narrator")

        # 去重并保持顺序
        seen = set()
        broadcast_targets = [t for t in broadcast_targets if not (t in seen or seen.add(t))]

        # 写入所有 targets 的历史（包括自己）
        for target in broadcast_targets:
            raw_path = self._get_raw_path(target)
            self._ensure_dir(raw_path)

            with open(raw_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(message, ensure_ascii=False) + "\n")

    def load_recent_history(self, agent_name: str, limit: int = 10) -> str:
        """
        加载某角色的最近对话历史

        Args:
            agent_name: 角色名
            limit: 返回最近多少条

        Returns:
            格式化的对话历史文本
        """
        raw_path = self._get_raw_path(agent_name)

        if not os.path.exists(raw_path):
            return ""

        lines = []
        with open(raw_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    lines.append(json.loads(line.strip()))

        # 取最近 limit 条
        recent = lines[-limit:]

        # 格式化为文本
        formatted = []
        for msg in recent:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if role == "player":
                formatted.append(f"玩家: {content}")
            else:
                formatted.append(f"{role}: {content}")

        return "\n".join(formatted)


# 全局实例
broadcaster = MessageBroadcaster()
