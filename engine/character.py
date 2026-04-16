"""游戏角色（Character）与旁白（Narrator）的运行封装。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable

from engine.agent_factory import get_conversation_agent
from engine.agent_runner import run_structured_agent
from engine.agent_schema import CharacterOutput, NarratorOutput, StateUpdaterOutput
from engine.prompt_builder import build_search_query, build_user_message
from llm.providers import get_llm_config, get_narrator_llm_config
from log_config.routing import routing_logger
from memory.retrieval import search_memories
from shared.config import AGENT_RUN_TIMEOUT_SECONDS, get_agent_names
from shared.text_utils import clean_response, get_display_name, is_valid_response
from storage.agent_files import (
    FileUpdateResult,
    add_pending_event,
    mark_event_triggered,
    read_agent_file,
    update_memory,
    update_player,
    update_status,
)
from storage.history import load_conversation_history

_FILE_UPDATES_EVENT = "agentgal.routing.file_updates"


def _apply_file_updates(agent_name: str, ops: list[tuple[str, Callable]]) -> None:
    """执行文件更新操作列表，捕获错误并记录结构化日志。"""
    results: list[FileUpdateResult] = []
    for label, fn in ops:
        try:
            results.append(fn())
        except Exception as e:
            routing_logger.error(f"[{agent_name}] {label} 失败: {e}")
    if results:
        routing_logger.debug(
            "[FileUpdate] 文件更新: agent=%s, count=%s",
            agent_name,
            len(results),
            extra={
                "event.name": _FILE_UPDATES_EVENT,
                "file_update.agent": agent_name,
                "file_update.count": len(results),
                "file_update.updates": results,
            },
        )


class Character:
    """可对话游戏角色。封装单次对话 run + 文件写回。"""

    def __init__(self, name: str) -> None:
        self.name = name

    @property
    def _agent(self):
        return get_conversation_agent(self.name)

    async def run(self, user_input: str, raw_messages: list[dict] | None = None) -> CharacterOutput:
        """搜记忆 → 构建 prompt → 运行 agent → 写回文件，返回 CharacterOutput。"""
        if raw_messages is None:
            raw_messages = load_conversation_history(limit=None)

        relevant_memories = search_memories(self.name, build_search_query(self.name, user_input))
        memory_prefix = (
            f"<relevant_memories>\n{relevant_memories}\n</relevant_memories>"
            if relevant_memories
            else ""
        )
        user_message, was_truncated = build_user_message(
            self.name, user_input, memory_prefix, raw_messages=raw_messages
        )
        if was_truncated:
            routing_logger.info("[%s] 历史窗口截断，触发记忆整理", self.name)
            from engine.consolidation_flow import memory_consolidation_flow
            asyncio.create_task(memory_consolidation_flow.consolidate_agent(self.name))

        config = get_llm_config()
        output = await run_structured_agent(
            agent=self._agent,
            user_input=user_message,
            output_type=CharacterOutput,
            timeout_seconds=AGENT_RUN_TIMEOUT_SECONDS,
            workflow_name="agentgal_turn",
            trace_metadata={"agent_name": self.name},
            usage_agent=self.name,
            usage_phase="agent_run",
            model_name=config["model"],
        )
        await self._apply_updates(output)
        return output

    async def _apply_updates(self, output: CharacterOutput) -> None:
        """将 CharacterOutput 的更新指令写回对应文件。"""
        ops: list[tuple[str, Callable]] = []

        if output.memory:
            ops.append(("memory", lambda: update_memory(self.name, output.memory)))

        for field, content in output.status.items():
            if not content:
                continue
            ops.append((f"status[{field}]", lambda fn=field, fc=content: update_status(self.name, fn, str(fc))))

        for field, content in output.player.items():
            ops.append((f"player[{field}]", lambda fn=field, fc=content: update_player(self.name, fn, str(fc))))

        for event_name in output.triggered:
            ops.append((f"triggered[{event_name}]", lambda en=event_name: mark_event_triggered(self.name, en, "打算")))

        for event_desc in output.add_event:
            if event_desc.strip() == "无":
                continue
            ops.append(("add_event", lambda ec=event_desc: add_pending_event(self.name, ec, "打算")))

        _apply_file_updates(self.name, ops)


class Narrator:
    """旁白。封装路由决策与 narrator 文件写回。"""

    name: str = "narrator"

    @property
    def _agent(self):
        return get_conversation_agent("narrator")

    async def route(
        self, user_input: str, raw_messages: list[dict] | None = None
    ) -> tuple[list[str], str, bool]:
        """运行 narrator → 返回 (targets, scene_description, is_valid)。"""
        valid_agents = get_agent_names(include_narrator=False)
        if raw_messages is None:
            raw_messages = load_conversation_history(limit=None)

        async def _run(narrator_input: str) -> tuple[list[str], str]:
            output = await self._run_narrator(narrator_input, raw_messages)
            valid_targets = [t for t in output.targets if t in valid_agents]
            scene = self._sanitize_scene_description(output.content)
            return valid_targets, scene

        async def _retry() -> tuple[list[str], str]:
            correction = (
                f"{user_input}\n\n"
                "<routing_correction>"
                "上一轮没有返回有效 targets。请保留玩家原意，重新输出 JSON；"
                "targets 必须包含至少 1 个 <fields> 中可回应的角色id。"
                "</routing_correction>"
            )
            return await _run(correction)

        retried = False
        try:
            targets, scene = await _run(user_input)
        except Exception as e:
            self._log_failure("首次调用", e)
            retried = True
            try:
                targets, scene = await _retry()
            except Exception as e:
                self._log_failure("重试调用", e)
                return [], "", False

        if not targets:
            if not retried:
                routing_logger.warning("narrator 响应缺少有效 TARGETS，重试中...")
                try:
                    targets, scene = await _retry()
                except Exception as e:
                    self._log_failure("重试调用", e)
                    return [], "", False
            if not targets:
                routing_logger.warning("narrator 重试后仍缺少有效 TARGETS")
                return [], scene, False

        return targets, scene, is_valid_response(scene, "narrator")

    async def apply_state_updates(self, output: StateUpdaterOutput) -> None:
        """将 StateUpdaterOutput 写回 narrator 文件。"""
        ops: list[tuple[str, Callable]] = []

        for field, content in output.status.model_dump().items():
            if not content:
                continue
            ops.append((f"status[{field}]", lambda fn=field, fc=content: update_status("narrator", fn, str(fc))))

        for event_name in output.triggered:
            ops.append((f"triggered[{event_name}]", lambda en=event_name: mark_event_triggered("narrator", en, "待触发事件")))

        for event_desc in output.add_event:
            if event_desc.strip() == "无":
                continue
            ops.append(("add_event", lambda ec=event_desc: add_pending_event("narrator", ec, "待触发事件")))

        _apply_file_updates("narrator", ops)

    async def _run_narrator(self, user_input: str, raw_messages: list[dict]) -> NarratorOutput:
        """构建 prompt → 运行 narrator agent，返回 NarratorOutput。"""
        user_message, _ = build_user_message("narrator", user_input, "", raw_messages=raw_messages)
        config = get_narrator_llm_config()
        return await run_structured_agent(
            agent=self._agent,
            user_input=user_message,
            output_type=NarratorOutput,
            timeout_seconds=AGENT_RUN_TIMEOUT_SECONDS,
            workflow_name="agentgal_turn",
            trace_metadata={"agent_name": "narrator"},
            usage_agent="narrator",
            usage_phase="agent_run",
            model_name=config["model"],
        )

    def _sanitize_scene_description(self, scene_description: str) -> str:
        """截断 narrator 输出中混入的角色发言，避免写入错误 canon。"""
        sanitized = clean_response(scene_description)
        valid_agents = get_agent_names(include_narrator=False)

        for agent_name in valid_agents:
            soul_content = read_agent_file(agent_name, "soul.md")
            names_to_check = [agent_name]
            display_name = get_display_name(agent_name, soul_content) if soul_content else None
            if display_name and display_name != agent_name:
                names_to_check.append(display_name)

            for name in names_to_check:
                for pattern in [
                    re.compile(rf"^{re.escape(name)}\s*[:：]", re.MULTILINE | re.IGNORECASE),
                    re.compile(rf"^##\s*{re.escape(name)}", re.MULTILINE | re.IGNORECASE),
                ]:
                    match = pattern.search(sanitized)
                    if match:
                        routing_logger.warning(
                            f"[narrator] 场景描述中检测到角色台词 '{name}'，已截断"
                        )
                        sanitized = sanitized[: match.start()].strip()
                        break

        return sanitized

    @staticmethod
    def _log_failure(stage: str, exc: Exception) -> None:
        if isinstance(exc, asyncio.TimeoutError):
            routing_logger.error(f"[narrator] {stage} 超时: {exc}")
        else:
            routing_logger.error(f"[narrator] {stage} 失败: {exc}")


_characters: dict[str, Character] = {}


def get_character(name: str) -> Character:
    if name not in _characters:
        _characters[name] = Character(name)
    return _characters[name]


narrator = Narrator()
