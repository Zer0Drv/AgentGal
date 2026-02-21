"""Agent 运行器 - 初始化 Agent 并处理消息广播"""

import asyncio
import json
import os
from datetime import datetime

from agno.agent import Agent

from .agent_logger import log_agent_run
from .config import get_agent_names, AGENT_RUN_TIMEOUT_SECONDS
from .llm import get_model
from .response_parser import parse_agent_response
from .routing_logger import routing_logger
from .tools import (
    _append_section_file,
    _read_title,
    _update_section_file,
    get_allowed_fields,
)


class AgentManager:
    """管理所有角色的 Agent 实例"""

    def __init__(self):
        self.agents: dict[str, Agent] = {}
        self._init_agents()

    def _init_agents(self):
        """初始化所有角色 Agent"""
        for agent_name in get_agent_names():
            self.agents[agent_name] = self._create_agent(agent_name)

    def _create_agent(self, agent_name: str) -> Agent:
        """创建单个 Agent"""
        # 加载静态角色设定（soul.md 是只读的）
        soul_content = self._load_agent_file(agent_name, "soul.md")

        # 定义动态 instructions 函数，每次运行时重新加载记忆文件
        def get_dynamic_instructions(agent: Agent) -> str:
            # 运行时加载完整 Memory.md（依赖 DeepSeek 前缀缓存降低成本）
            memory_content = self._load_full_memory(agent_name)

            # 加载 status.md
            status_content = self._load_agent_file(agent_name, "status.md")

            # 加载 user.md
            user_content = self._load_agent_file(agent_name, "user.md")

            # 加载并填充 system prompt 模板
            prompt_template = self._load_system_prompt_template(agent_name)
            # 动态获取字段白名单（从文件读取，失败回退到默认值）
            status_fields = "、".join(get_allowed_fields(agent_name, "status"))
            player_fields = "、".join(get_allowed_fields(agent_name, "user"))
            return prompt_template.format(
                agent_name=agent_name,
                soul=soul_content,
                memory=memory_content if memory_content else "（尚无长期记忆）",
                status=status_content if status_content else "（尚无状态记录）",
                user_profile=user_content if user_content else "（尚无玩家认知）",
                status_fields=status_fields,
                player_fields=player_fields,
            )

        return Agent(
            name=agent_name,
            model=get_model(),
            instructions=get_dynamic_instructions,
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

    def _load_full_memory(self, agent_name: str) -> str:
        """加载完整 Memory.md 注入 system prompt。

        全量加载，依赖 DeepSeek 前缀缓存降低成本。
        """
        path = f"agents/{agent_name}/memory/Memory.md"
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()

    async def run_agent(self, agent_name: str, user_input: str) -> str:
        agent = self.agents.get(agent_name)
        if not agent:
            return f"[错误: 未找到角色 {agent_name}]"

        import time

        start = time.time()
        try:
            response = await asyncio.wait_for(
                agent.arun(user_input),
                timeout=AGENT_RUN_TIMEOUT_SECONDS,
            )
            elapsed = time.time() - start
            routing_logger.info(f"{agent_name} 运行完成，耗时 {elapsed:.1f}秒")

            # 解析响应中的 XML 更新指令
            raw_content = response.content
            parsed = parse_agent_response(raw_content, agent_name)

            # 应用更新到文件
            await self._apply_response_updates(agent_name, parsed)

            return parsed.content
        except asyncio.TimeoutError:
            elapsed = time.time() - start
            routing_logger.error(f"{agent_name} 运行超时（{elapsed:.1f}秒），强制终止")
            return f"[{agent_name} 回应超时，请稍后再试]"

    async def _apply_response_updates(self, agent_name: str, parsed) -> None:
        """
        应用解析后的更新到对应文件。

        Args:
            agent_name: 角色名称
            parsed: ParsedResponse 对象
        """
        results = []

        # --- memory: 追加到 Memory.md（带去重） ---
        if parsed.memory:
            try:
                result = self._update_memory(agent_name, parsed.memory)
                results.append(f"memory: {result}")
            except Exception as e:
                routing_logger.error(f"[{agent_name}] 更新 memory 失败: {e}")
                results.append(f"memory: 失败")

        # --- status: 覆盖更新到 status.md ---
        if parsed.status:
            try:
                for field, content in parsed.status.items():
                    result = self._update_status(agent_name, field, str(content))
                    results.append(f"status[{field}]: {result}")
            except Exception as e:
                routing_logger.error(f"[{agent_name}] 更新 status 失败: {e}")
                results.append(f"status: 失败")

        # --- player: 追加更新到 user.md ---
        if parsed.player:
            try:
                for field, content in parsed.player.items():
                    result = self._update_player(agent_name, field, str(content))
                    results.append(f"player[{field}]: {result}")
            except Exception as e:
                routing_logger.error(f"[{agent_name}] 更新 player 失败: {e}")
                results.append(f"player: 失败")

        if results:
            routing_logger.info(f"[{agent_name}] 文件更新: {'; '.join(results)}")

    def _update_memory(self, agent_name: str, memory_content: str) -> str:
        """追加 memory 内容到 Memory.md（带去重）"""
        if not memory_content or not memory_content.strip():
            return "内容为空，跳过"

        memory_path = f"agents/{agent_name}/memory/Memory.md"
        os.makedirs(os.path.dirname(memory_path), exist_ok=True)

        clean = memory_content.replace("\\n", "\n").strip()

        # 解析 entries
        def _parse_entries(text: str) -> list[str]:
            entries = []
            current_entry = []
            for line in text.split("\n"):
                if line.strip().startswith("##") or (
                    line.strip().startswith("-") and "**" in line
                ):
                    if current_entry:
                        entries.append("\n".join(current_entry).strip())
                    current_entry = [line]
                elif line.strip() or current_entry:
                    current_entry.append(line)
            if current_entry:
                entries.append("\n".join(current_entry).strip())
            return entries

        # 读取现有内容
        existing = ""
        if os.path.exists(memory_path):
            with open(memory_path, "r", encoding="utf-8") as f:
                existing = f.read()

        new_entries = _parse_entries(clean)
        existing_entries = _parse_entries(existing)
        existing_set = set(existing_entries)

        # 去重
        unique_entries = [e for e in new_entries if e and e not in existing_set]

        if not unique_entries:
            return "所有 entry 已存在，跳过"

        # 写入
        to_append = "\n\n".join(unique_entries)
        if existing.strip():
            with open(memory_path, "a", encoding="utf-8") as f:
                f.write(f"\n\n{to_append}")
        else:
            with open(memory_path, "w", encoding="utf-8") as f:
                f.write(f"# {agent_name} 的长期记忆\n\n{to_append}")

        msg = f"已追加 {len(unique_entries)} 个新 entry"
        routing_logger.info(f"[{agent_name}] {msg}")
        return msg

    def _update_status(self, agent_name: str, field: str, content: str) -> str:
        """覆盖更新 status.md 的指定字段"""
        allowed = get_allowed_fields(agent_name, "status")
        if field not in allowed:
            routing_logger.warning(
                f"[{agent_name}] 不允许的 status 字段: {field}, "
                f"允许的: {', '.join(allowed)}"
            )
            return f"字段 {field} 不在白名单中"

        status_path = f"agents/{agent_name}/status.md"
        title = _read_title(status_path, "# 我的状态")

        result = _update_section_file(status_path, field, content, allowed, title)
        return result

    def _update_player(self, agent_name: str, field: str, content: str) -> str:
        """追加更新 user.md 的指定字段"""
        allowed = get_allowed_fields(agent_name, "user")
        if field not in allowed:
            routing_logger.warning(
                f"[{agent_name}] 不允许的 player 字段: {field}, "
                f"允许的: {', '.join(allowed)}"
            )
            return f"字段 {field} 不在白名单中"

        user_path = f"agents/{agent_name}/user.md"
        title = _read_title(user_path, "# 玩家档案")

        result = _append_section_file(user_path, field, content, allowed, title)
        return result


# 全局实例
agent_manager = AgentManager()


class MessageBroadcaster:
    """消息广播系统 - 维护每个角色的独立对话历史"""

    def __init__(self):
        self.agents = get_agent_names()

    def _get_raw_path(self, agent_name: str, date: str = None) -> str:
        """获取某角色的 raw 对话文件路径"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        return f"agents/{agent_name}/memory/raw/{date}.jsonl"

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
            lines = [msg for msg in lines if agent_name in msg.get("visible_to", [])]

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
