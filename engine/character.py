"""游戏角色（Character）与旁白（Narrator）的运行封装。

两者都继承自 BaseEntity，封装自己的 soul/status 读写与 SDK 调用；
写入统一走实体方法（set_status_fields / append_memory / set_relation / ...），
不再让外部直接调用底层 update_xxx。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, ClassVar

from agents.factory import (
    get_conversation_agent,
    get_state_updater_agent,
)
from agents.runner import run_structured_agent
from agents.schema import (
    CharacterOutput,
    CharacterSchedule,
    NarratorOutput,
    NewCharacterRequest,
    StateUpdaterOutput,
)
from engine.prompt_builder import (
    build_schedule_snapshot,
    build_search_query,
    build_user_message,
)
from llm.providers import get_llm_config, get_narrator_llm_config
from log_config.routing import routing_logger
from memory.parser import extract_status_field, normalize
from memory.retrieval import search_memories
from shared.config import AGENT_RUN_TIMEOUT_SECONDS, get_agent_names
from shared.text_utils import (
    clean_response,
    get_display_name,
    is_valid_response,
    role_to_speaker,
)
from storage.agent_files import (
    FileUpdateResult,
    add_pending_event,
    append_memory_draft,
    mark_event_triggered,
    read_agent_file,
    read_turn_counter,
    update_player,
    update_relations,
    update_status,
    update_status_allow_new_field,
)
from storage.history import load_conversation_history
from world.schedule import load_character_schedule


_FILE_UPDATES_EVENT = "agentgal.routing.file_updates"

# 由 add_event / mark_triggered 逐条维护，禁止通过 set_status_fields 整段覆写
_EVENT_SECTION_FIELDS = {"打算", "待触发事件"}


def _log_file_updates(agent_name: str, results: list[FileUpdateResult]) -> None:
    """批量记录一次回合的文件更新结果，供观测使用。"""
    if not results:
        return
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


class BaseEntity:
    """Character 与 Narrator 的共同祖先。

    聚合 soul / status 的读写与 SDK 调用；子类通过 `_EVENT_SECTION` 类属性
    指定自己的事件段名（Character=打算 / Narrator=待触发事件），这样
    `add_event` / `mark_triggered` 可以在基类里统一实现。
    """

    _EVENT_SECTION: ClassVar[str] = ""

    def __init__(self, name: str) -> None:
        self.name = name
        self._soul_cache: str | None = None

    # ── 读（property）──

    @property
    def soul(self) -> str:
        """手写定义；缓存到实例，整个存档生命周期不会变。"""
        if self._soul_cache is None:
            self._soul_cache = read_agent_file(self.name, "soul.md")
        return self._soul_cache

    @property
    def display_name(self) -> str:
        return get_display_name(self.name, self.soul)

    @property
    def status(self) -> str:
        """每次访问实时从磁盘读；status.md 是运行时动态文件。"""
        return read_agent_file(self.name, "status.md")

    @property
    def _sdk(self) -> Any:
        """SDK 层的 pydantic-ai Agent 实例；子类必须覆写。"""
        raise NotImplementedError

    # ── 写（方法）──

    def set_status_fields(self, fields: dict[str, Any]) -> list[FileUpdateResult]:
        """按字段合并更新 status.md。事件段（打算 / 待触发事件）禁止从此路径整段覆写。"""
        results: list[FileUpdateResult] = []
        for field, content in fields.items():
            if not content:
                continue
            if field in _EVENT_SECTION_FIELDS:
                routing_logger.warning(
                    "[%s] set_status_fields 拒绝写入事件段 %r；请用 add_event / mark_triggered",
                    self.name,
                    field,
                )
                continue
            try:
                results.append(update_status(self.name, field, str(content)))
            except Exception as e:
                routing_logger.error(f"[{self.name}] status[{field}] 失败: {e}")
        return results

    def add_event(self, desc: str) -> FileUpdateResult | None:
        """向事件段追加一条待触发事件；desc 为 '无' 时跳过。"""
        if desc.strip() == "无":
            return None
        try:
            return add_pending_event(self.name, desc, self._EVENT_SECTION)
        except Exception as e:
            routing_logger.error(f"[{self.name}] add_event 失败: {e}")
            return None

    def mark_triggered(self, event_name: str) -> FileUpdateResult | None:
        """从事件段移除一条已触发事件。"""
        try:
            return mark_event_triggered(self.name, event_name, self._EVENT_SECTION)
        except Exception as e:
            routing_logger.error(f"[{self.name}] mark_triggered 失败: {e}")
            return None

    def reload(self) -> None:
        """存档恢复 / reset 后调用，清空 soul 缓存。"""
        self._soul_cache = None

    async def _run_structured(
        self,
        user_message: str,
        output_type: type,
        config: dict,
        workflow_name: str,
        *,
        sdk: Any = None,
        usage_agent: str | None = None,
    ):
        """执行结构化 SDK 调用的共用封装。"""
        return await run_structured_agent(
            agent=sdk if sdk is not None else self._sdk,
            user_input=user_message,
            output_type=output_type,
            timeout_seconds=AGENT_RUN_TIMEOUT_SECONDS,
            workflow_name=workflow_name,
            trace_metadata={"agent_name": usage_agent or self.name},
            usage_agent=usage_agent or self.name,
            usage_phase="agent_run",
            model_name=config["model"],
        )


class Character(BaseEntity):
    """可对话游戏角色。封装单次对话 run + 文件写回。"""

    _EVENT_SECTION: ClassVar[str] = "打算"

    @property
    def _sdk(self) -> Any:
        return get_conversation_agent(self.name)

    # ── Character 独有的动态文件（全部实时读）──

    @property
    def memory(self) -> str:
        return read_agent_file(self.name, "memory.md")

    @property
    def relations(self) -> str:
        return read_agent_file(self.name, "relations.md")

    @property
    def user_profile(self) -> str:
        return read_agent_file(self.name, "user.md")

    @property
    def growth(self) -> str:
        return read_agent_file(self.name, "growth.md")

    @property
    def schedule(self) -> CharacterSchedule:
        return load_character_schedule(self.name)

    # ── Character 独有的写入方法 ──

    def append_memory(self, text: str) -> FileUpdateResult | None:
        """把本轮 memory 片段追加到 memory_draft.jsonl，等待后续 consolidation 归并。

        每条 draft 带上当前全局 turn 号，供 EpisodeClosureDetector 确认闭合 turn 后切片归并。
        """
        if not text:
            return None
        normalized = normalize(text)
        if not normalized:
            return None
        try:
            turn = read_turn_counter()
            append_memory_draft(self.name, turn, normalized)
            return FileUpdateResult(
                file="memory_draft.jsonl",
                target="长期记忆",
                operation="append",
                appended=normalized,
            )
        except Exception as e:
            routing_logger.error(f"[{self.name}] memory_draft 写入失败: {e}")
            return None

    def set_user_profile_fields(self, fields: dict[str, Any]) -> list[FileUpdateResult]:
        """批量追加到 tmp_user.md 的各字段（首次写入时从 user.md 复制草稿）。"""
        results: list[FileUpdateResult] = []
        for field, content in fields.items():
            try:
                results.append(update_player(self.name, field, str(content)))
            except Exception as e:
                routing_logger.error(f"[{self.name}] player[{field}] 失败: {e}")
        return results

    def set_relation(self, target_display: str, content: str) -> FileUpdateResult | None:
        try:
            return update_relations(self.name, target_display, str(content))
        except Exception as e:
            routing_logger.error(f"[{self.name}] relations[{target_display}] 失败: {e}")
            return None

    # ── 对话主流程 ──

    async def run(
        self,
        user_input: str,
        raw_messages: list[dict] | None = None,
    ) -> CharacterOutput:
        """搜记忆 → 构建 prompt → 运行 SDK → 写回文件，返回 CharacterOutput。"""
        if raw_messages is None:
            raw_messages = load_conversation_history(limit=None)

        user_message = self._build_prompt(user_input, raw_messages)

        config = get_llm_config()
        output = await self._run_structured(
            user_message=user_message,
            output_type=CharacterOutput,
            config=config,
            workflow_name="agentgal_turn",
        )
        await self._apply_updates(output)
        return output

    def _build_prompt(
        self, user_input: str, raw_messages: list[dict]
    ) -> str:
        """组装角色 user message（含记忆召回前缀）。"""
        relevant_memories = search_memories(
            self.name, build_search_query(self.name, user_input)
        )
        memory_prefix = (
            f"<relevant_memories>\n{relevant_memories}\n</relevant_memories>"
            if relevant_memories
            else ""
        )
        message, _ = build_user_message(
            self.name,
            user_input,
            memory_prefix,
            raw_messages=raw_messages,
        )
        return message

    async def _apply_updates(self, output: CharacterOutput) -> None:
        """把 CharacterOutput 的所有字段落盘，并一次性记录结构化日志。"""
        results: list[FileUpdateResult] = []

        if output.memory:
            r = self.append_memory(output.memory)
            if r is not None:
                results.append(r)

        results.extend(self.set_status_fields(output.status))
        results.extend(self.set_user_profile_fields(output.player))

        for event_name in output.triggered:
            r = self.mark_triggered(event_name)
            if r is not None:
                results.append(r)

        for event_desc in output.add_event:
            r = self.add_event(event_desc)
            if r is not None:
                results.append(r)

        valid_relation_targets = {
            get_display_name(name, read_agent_file(name, "soul.md"))
            for name in get_agent_names(include_narrator=False)
            if name != self.name
        }
        for target, content in output.relations.items():
            target_clean = target.strip()
            if not target_clean or target_clean == "player":
                continue
            if target_clean not in valid_relation_targets:
                routing_logger.warning(
                    "[%s] 忽略 relations 中的未知目标: %s", self.name, target_clean
                )
                continue
            r = self.set_relation(target_clean, content)
            if r is not None:
                results.append(r)

        _log_file_updates(self.name, results)


class Narrator(BaseEntity):
    """旁白。封装路由决策、state_updater 调度与 narrator 文件写回。"""

    _EVENT_SECTION: ClassVar[str] = "待触发事件"
    _PLAYER_RELATION_SECTION: ClassVar[str] = "和玩家的关系"

    def __init__(self) -> None:
        super().__init__(name="narrator")

    @property
    def _sdk(self) -> Any:
        return get_conversation_agent("narrator")

    @property
    def _state_updater_sdk(self) -> Any:
        return get_state_updater_agent()

    async def route(
        self, user_input: str, raw_messages: list[dict] | None = None
    ) -> tuple[list[str], str, list[NewCharacterRequest], bool]:
        """运行 narrator → 返回 (targets, scene_description, new_characters, is_valid)。

        new_characters 是 narrator 本轮请求孵化的新角色 spec 列表；
        此处只做 schema 层过滤（保留 relation_to 合法且描述非空的锚点），
        实际命名、孵化与目录校验由 engine.character_factory.create_character 负责。
        """
        self.sync_player_relations()
        valid_agents = get_agent_names(include_narrator=False)
        if raw_messages is None:
            raw_messages = load_conversation_history(limit=None)

        async def _run(
            narrator_input: str,
        ) -> tuple[list[str], str, list[NewCharacterRequest]]:
            output = await self._run_narrator(narrator_input, raw_messages)
            new_chars = self._filter_new_characters(output.new_characters, valid_agents)
            valid_targets = [t for t in output.targets if t in valid_agents]
            scene = self._sanitize_scene_description(output.content)
            return valid_targets, scene, new_chars

        async def _retry() -> tuple[list[str], str, list[NewCharacterRequest]]:
            correction = (
                f"{user_input}\n\n"
                "<routing_correction>"
                "上一轮没有返回可用路由。请保留玩家原意，重新输出 JSON；"
                "如果本轮已有现成主要角色可回应，targets 必须包含至少 1 个 <fields> 中的角色id；"
                "如果本轮要引入新角色，targets 可以暂时为空，但必须提供合法的 new_characters 锚点。"
                "</routing_correction>"
            )
            return await _run(correction)

        retried = False
        try:
            targets, scene, new_chars = await _run(user_input)
        except Exception as e:
            self._log_failure("首次调用", e)
            retried = True
            try:
                targets, scene, new_chars = await _retry()
            except Exception as e:
                self._log_failure("重试调用", e)
                return [], "", [], False

        if not targets and not new_chars:
            if not retried:
                routing_logger.warning("narrator 响应缺少可用路由，重试中...")
                try:
                    targets, scene, new_chars = await _retry()
                except Exception as e:
                    self._log_failure("重试调用", e)
                    return [], "", [], False
            if not targets and not new_chars:
                routing_logger.warning("narrator 重试后仍缺少可用路由")
                return [], scene, new_chars, False

        return targets, scene, new_chars, is_valid_response(scene, "narrator") and bool(
            targets or new_chars
        )

    @staticmethod
    def _filter_new_characters(
        specs: list[NewCharacterRequest],
        existing_agents: list[str],
    ) -> list[NewCharacterRequest]:
        """过滤 narrator 提交的 new_characters：去重、去空、去非法 relation_to。"""
        valid_anchors = set(existing_agents) | {"player"}
        kept: list[NewCharacterRequest] = []
        seen: set[tuple[str, str, str]] = set()
        for spec in specs:
            name_hint = spec.name_hint.strip()
            relation_to = spec.relation_to.strip()
            description = spec.relation_description.strip()
            dedupe_key = (name_hint, relation_to, description)
            label = name_hint or description or "（未命名新角色）"
            if dedupe_key in seen:
                continue
            if relation_to not in valid_anchors:
                routing_logger.warning(
                    f"[narrator] new_characters 中 {label!r} 的 relation_to={relation_to!r} 不合法，跳过"
                )
                continue
            if not description:
                routing_logger.warning(
                    f"[narrator] new_characters 中 {label!r} 缺 relation_description，跳过"
                )
                continue
            kept.append(
                NewCharacterRequest(
                    name_hint=name_hint,
                    relation_to=relation_to,
                    relation_description=description,
                    background_hint=spec.background_hint.strip(),
                    initial_location=spec.initial_location.strip(),
                )
            )
            seen.add(dedupe_key)
        return kept

    # ── 回合结束后维护世界状态（原 conversation_flow.run_state_updater）──

    async def update_state(self) -> None:
        """调用 state_updater 写回 narrator/status.md。"""
        self.sync_player_relations()
        user_message = self._build_state_updater_input()
        config = get_narrator_llm_config()
        try:
            output = await self._run_structured(
                user_message=user_message,
                output_type=StateUpdaterOutput,
                config=config,
                workflow_name="agentgal_state_update",
                sdk=self._state_updater_sdk,
                usage_agent="state_updater",
            )
        except Exception as e:
            routing_logger.error(f"[state_updater] 运行失败: {e}")
            return

        self._apply_state_updates(output)

    def sync_player_relations(self) -> FileUpdateResult:
        """把各角色 status.md 的「和玩家的关系」汇总到 narrator/status.md。"""
        content = self._format_player_relations()
        result = update_status_allow_new_field(
            self.name,
            self._PLAYER_RELATION_SECTION,
            content,
        )
        _log_file_updates(self.name, [result])
        return result

    @classmethod
    def _format_player_relations(cls) -> str:
        lines: list[str] = []
        for agent_name in get_agent_names(include_narrator=False):
            status_content = read_agent_file(agent_name, "status.md")
            relation = extract_status_field(status_content, cls._PLAYER_RELATION_SECTION)
            relation = " ".join(relation.split())
            if not relation:
                continue
            soul_content = read_agent_file(agent_name, "soul.md")
            display_name = get_display_name(agent_name, soul_content)
            lines.append(f"- {display_name}：{relation}")
        return "\n".join(lines) if lines else "（暂无）"

    def _build_state_updater_input(self) -> str:
        narrator_status = self.status
        game_time = extract_status_field(narrator_status, "当前时间").strip()
        schedule_snapshot = build_schedule_snapshot(game_time)

        character_intention = self._format_character_intentions()
        raw_messages = load_conversation_history(limit=3)
        history_lines: list[str] = []
        for msg in raw_messages:
            role = msg.get("role", "unknown")
            content = (msg.get("content") or "").strip()
            if not content:
                continue
            content = "\n".join(line.rstrip() for line in content.splitlines())
            if len(content) > 900:
                content = content[:900].rstrip() + "..."
            history_lines.append(f"{role_to_speaker(role)}: {content}")
        recent_history = "\n\n".join(history_lines) if history_lines else "无"

        parts: list[str] = []
        if schedule_snapshot:
            parts.append(schedule_snapshot)
        parts.append(f"<character_intention>\n{character_intention}\n</character_intention>")
        parts.append(f"<current_narrator_status>\n{narrator_status}\n</current_narrator_status>")
        parts.append(f"<recent_history>\n{recent_history}\n</recent_history>")
        return "\n\n---\n\n".join(parts)

    @staticmethod
    def _format_character_intentions() -> str:
        """提取所有角色的「打算」，供 state_updater 同步到公共待触发事件。"""
        blocks: list[str] = []
        for agent_name in get_agent_names(include_narrator=False):
            status_content = read_agent_file(agent_name, "status.md")
            intentions = extract_status_field(status_content, "打算").strip() or "（暂无）"
            soul_content = read_agent_file(agent_name, "soul.md")
            display_name = get_display_name(agent_name, soul_content)
            blocks.append(f"【{agent_name} / {display_name}】\n{intentions}")
        return "\n\n".join(blocks) if blocks else "无"

    def _apply_state_updates(self, output: StateUpdaterOutput) -> None:
        """把 StateUpdaterOutput 的字段 / 触发 / 新增事件落盘，并记录结构化日志。"""
        results: list[FileUpdateResult] = []
        results.extend(self.set_status_fields(output.status.model_dump()))

        for event_name in output.triggered:
            r = self.mark_triggered(event_name)
            if r is not None:
                results.append(r)

        for event_desc in output.add_event:
            r = self.add_event(event_desc)
            if r is not None:
                results.append(r)

        _log_file_updates(self.name, results)

    async def _run_narrator(self, user_input: str, raw_messages: list[dict]) -> NarratorOutput:
        """构建 prompt → 运行 narrator SDK，返回 NarratorOutput。"""
        user_message, _ = build_user_message(
            self.name, user_input, "", raw_messages=raw_messages
        )
        config = get_narrator_llm_config()
        return await self._run_structured(
            user_message=user_message,
            output_type=NarratorOutput,
            config=config,
            workflow_name="agentgal_turn",
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


def reset_entities() -> None:
    """存档恢复 / reset 后调用：清空角色句柄缓存、刷新 narrator 的 soul 缓存。"""
    _characters.clear()
    narrator.reload()


narrator = Narrator()
