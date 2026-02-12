"""Agent 运行器 - 初始化 Agent 并处理消息广播"""

import os
import json
from datetime import datetime
from agno.agent import Agent

from .llm import get_model
from .tools import create_tools_for_agent
from .agent_logger import log_agent_run
from .routing_logger import routing_logger


class AgentManager:
    """管理所有角色的 Agent 实例"""

    def __init__(self):
        self.agents: dict[str, Agent] = {}
        self._init_agents()

    def _init_agents(self):
        """初始化所有角色 Agent"""
        for agent_name in ["lilith", "mitsuki", "narrator"]:
            self.agents[agent_name] = self._create_agent(agent_name)

    def _create_agent(self, agent_name: str) -> Agent:
        """创建单个 Agent"""
        # 加载静态角色设定（soul.md 是只读的）
        soul_content = self._load_agent_file(agent_name, "soul.md")

        # 定义动态 instructions 函数，每次运行时重新加载记忆文件
        def get_dynamic_instructions(agent: Agent) -> str:
            # 运行时加载 memory.md 最后 N 行作为近期记忆
            max_lines = int(os.getenv("MEMORY_CONTEXT_LINES", "30"))
            memory_content = self._load_recent_memory(agent_name, max_lines=max_lines)

            # 加载 status.md
            status_content = self._load_agent_file(agent_name, "status.md")

            # 加载并填充 system prompt 模板
            prompt_template = self._load_system_prompt_template(agent_name)
            return prompt_template.format(
                agent_name=agent_name,
                soul=soul_content,
                memory=memory_content if memory_content else "（尚无长期记忆）",
                status=status_content if status_content else "（尚无状态记录）",
            )

        # 为该角色创建专属工具（已绑定 agent_name）
        tools = create_tools_for_agent(agent_name)

        return Agent(
            name=agent_name,
            model=get_model(),
            instructions=get_dynamic_instructions,
            tools=tools,
            markdown=True,
            post_hooks=[log_agent_run],
            # 禁用 Agno 内部历史管理，由应用层通过 jsonl 自行管理
            add_history_to_context=False,
        )

    def _load_system_prompt_template(self, agent_name: str) -> str:
        """加载 system prompt 模板"""
        # narrator 使用专用模板
        if agent_name == "narrator":
            template_path = "prompts/narrator_prompt.txt"
        else:
            template_path = "prompts/character_prompt.txt"

        with open(template_path, "r", encoding="utf-8") as f:
            return f.read()

    def _load_agent_file(self, agent_name: str, filename: str) -> str:
        """加载角色目录下的指定文件"""
        path = f"agents/{agent_name}/{filename}"
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def _load_recent_memory(self, agent_name: str, max_lines: int = 30) -> str:
        """加载 memory.md 最后 N 行作为近期记忆注入 system prompt。

        完整记忆通过 search_memory tool 按需检索。
        """
        path = f"agents/{agent_name}/memory/Memory.md"
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if not lines:
            return ""
        # 取最后 max_lines 行
        recent = lines[-max_lines:]
        return "".join(recent).strip()

    async def run_agent(self, agent_name: str, user_input: str) -> str:
        agent = self.agents.get(agent_name)
        if not agent:
            return f"[错误: 未找到角色 {agent_name}]"

        import time
        start = time.time()
        response = await agent.arun(user_input)
        elapsed = time.time() - start
        routing_logger.info(f"{agent_name} 运行完成，耗时 {elapsed:.1f}秒")
        return response.content


# 全局实例
agent_manager = AgentManager()


class MessageBroadcaster:
    """消息广播系统 - 维护每个角色的独立对话历史"""

    def __init__(self):
        self.agents = ["lilith", "mitsuki", "narrator"]

    def _get_raw_path(self, agent_name: str, date: str = None) -> str:
        """获取某角色的 raw 对话文件路径"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        return f"agents/{agent_name}/memory/raw/{date}.jsonl"

    def _ensure_dir(self, path: str):
        """确保目录存在"""
        os.makedirs(os.path.dirname(path), exist_ok=True)

    async def _broadcast_message(
        self,
        targets: list[str],
        message: dict,
    ):
        """统一的消息广播方法

        Args:
            targets: 原始目标角色列表
            message: 要广播的消息字典
        """
        # narrator 作为 DM 需要看到所有消息（上帝视角）
        broadcast_targets = targets.copy()
        if "narrator" not in broadcast_targets:
            broadcast_targets.append("narrator")

        # 去重并保持顺序
        seen = set()
        broadcast_targets = [t for t in broadcast_targets if not (t in seen or seen.add(t))]

        # 更新消息的 visible_to 为实际广播目标
        message["visible_to"] = broadcast_targets

        # 写入所有 targets 的 jsonl
        for target in broadcast_targets:
            raw_path = self._get_raw_path(target)
            self._ensure_dir(raw_path)

            with open(raw_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(message, ensure_ascii=False) + "\n")

    async def broadcast_player_message(
        self, targets: list[str], content: str
    ):
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

    def load_recent_history(self, agent_name: str, limit: int = 10) -> str:
        """
        加载某角色的最近对话历史

        统一从 narrator 的 jsonl 读取（上帝视角，最完整），
        然后按 visible_to 字段过滤该角色可见的消息。

        Args:
            agent_name: 角色名
            limit: 返回最近多少条

        Returns:
            格式化的对话历史文本
        """
        # 统一从 narrator 的 jsonl 读取
        raw_path = self._get_raw_path("narrator")

        if not os.path.exists(raw_path):
            return ""

        lines = []
        with open(raw_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    lines.append(json.loads(line.strip()))

        # 按 visible_to 过滤：narrator 看全部，其他角色只看自己可见的
        if agent_name != "narrator":
            lines = [
                msg for msg in lines
                if agent_name in msg.get("visible_to", [])
            ]

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
