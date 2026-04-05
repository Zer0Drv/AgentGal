"""后台记忆整理流程。"""

import asyncio
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from engine.agent_factory import (
    get_growth_dedup_agent,
    get_growth_extract_agent,
    get_memory_merge_agent,
    get_memory_metadata_agent,
    get_player_profile_agent,
)
from engine.agent_runner import run_structured_agent, run_text_agent
from log_config.memory import memory_logger as routing_logger
from llm.providers import get_consolidation_llm_config
from shared.config import (
    CONSOLIDATION_TEMPERATURE,
    GROWTH_DEDUP_THRESHOLD,
    HISTORY_HIGH,
    character_path,
)
from engine.prompt_builder import (
    build_memory_merge_payload,
    format_raw_dialogue_for_owner,
)
from storage.agent_files import (
    get_fields_from_file,
    backup_file,
    read_growth_entries,
    read_agent_file,
    write_growth_entries,
)
from memory.parser import (
    extract_event_field,
    flatten_sections,
    group_blocks,
    is_structured_memory_block,
    make_block,
    normalize,
    parse_event_importance,
    render_sections,
    split_by_date,
    split_into_events,
)
from storage.vector_store import vector_store
from engine.agent_schema import (
    GrowthDedupOutput,
    GrowthExtractOutput,
    MemoryMergeEvent,
    MemoryMergeOutput,
    MemoryMetadataOutput,
)


# ---------------------------------------------------------------------------
# 辅助函数（非 LLM）
# ---------------------------------------------------------------------------


def safe_write_memory(
    path: Path,
    sections: dict[str, str],
    agent_name: str,
    original_content: str,
) -> tuple[int, int]:
    """安全写回 memory.md，带最小并发保护。"""
    current_content = path.read_text(encoding="utf-8")

    if current_content.startswith(original_content):
        appended = current_content[len(original_content):]
        if appended:
            routing_logger.info(
                f"[整理器] {agent_name} 检测到并发尾部追加 ({len(appended)} 字符)，将保留"
            )
    elif current_content == original_content:
        appended = ""
    else:
        routing_logger.warning(
            f"[整理器] {agent_name} 检测到并发中间变更，已放弃写回以避免覆盖（建议稍后重试）"
        )
        return -1, -1

    result = f"# {agent_name} 的长期记忆\n\n{render_sections(sections)}\n"
    consolidated_len = len(result)

    if appended:
        result += appended

    path.write_text(result, encoding="utf-8")
    return len(result), consolidated_len


_USER_FIELD_DESCRIPTIONS: dict[str, str] = {
    "基本信息": "优先保留已确认的客观信息：姓名、年龄、性别/称呼、身份",
    "对方是什么人": "最多 8 条：跨情境成立的性格、习惯、边界方式与行事风格（主语是\"对方\"），不要重复基本信息",
    "我们怎么相处": "最多 5 条：我和对方之间反复出现的双向互动规律（主语是\"我们/我和对方\"）",
}
_USER_SECTION_BULLET_LIMITS: dict[str, int] = {
    "对方是什么人": 8,
    "我们怎么相处": 5,
}


def build_fields_definition(agent_name: str) -> str:
    file_path = character_path(agent_name, "user.md")
    fields = get_fields_from_file(file_path) or ["基本信息", "对方是什么人", "我们怎么相处"]
    return "\n".join(
        f"- 「{f}」：{_USER_FIELD_DESCRIPTIONS.get(f, '')}" for f in fields
    )


def _build_player_profile_draft_input(draft_profile: str) -> str:
    return (
        "<draft_profile>\n"
        f"{draft_profile.strip()}\n"
        "</draft_profile>"
    )



def _split_section_bullets(section_text: str) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    for line in section_text.splitlines():
        if line.startswith("- "):
            if current:
                items.append("\n".join(current).strip())
            current = [line]
            continue
        if current:
            current.append(line)
    if current:
        items.append("\n".join(current).strip())
    return [item for item in items if item.strip() and item.strip() != "-"]


def _enforce_user_section_limits(content: str) -> str:
    lines = content.splitlines()
    rebuilt: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        if not line.startswith("## "):
            rebuilt.append(line)
            index += 1
            continue

        section_title = line[3:].strip()
        rebuilt.append(line)
        index += 1

        section_lines: list[str] = []
        while index < len(lines) and not lines[index].startswith("## "):
            section_lines.append(lines[index])
            index += 1

        limit = _USER_SECTION_BULLET_LIMITS.get(section_title)
        if limit is None:
            rebuilt.extend(section_lines)
            continue

        bullets = _split_section_bullets("\n".join(section_lines))
        if len(bullets) <= limit:
            rebuilt.extend(section_lines)
            continue

        rebuilt.append("")
        for item in bullets[:limit]:
            rebuilt.extend(item.splitlines())
        rebuilt.append("")

    return "\n".join(rebuilt).strip()


def _apply_chunk_metadata(event_text: str, keywords: str, importance: int) -> str:
    normalized_keywords = " ".join((keywords or "").split())
    normalized_importance = max(1, min(5, int(importance)))
    rewritten: list[str] = []
    inserted = False

    for line in (event_text or "").strip().splitlines():
        stripped = line.strip()
        if re.match(r"^(?:-\s*)?\*\*(关键词|重要度)\*\*：", stripped):
            continue
        if not inserted and re.match(r"^(?:-\s*)?\*\*内容\*\*：", stripped):
            rewritten.append(f"- **关键词**：{normalized_keywords}".rstrip())
            rewritten.append(f"- **重要度**：{normalized_importance}")
            inserted = True
        rewritten.append(line)

    if not inserted:
        rewritten.append(f"- **关键词**：{normalized_keywords}".rstrip())
        rewritten.append(f"- **重要度**：{normalized_importance}")

    return "\n".join(rewritten).strip()


def _apply_default_chunk_metadata(blocks: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        make_block(
            block["date"],
            _apply_chunk_metadata(
                block["content"],
                extract_event_field(block["content"], "关键词"),
                parse_event_importance(block["content"], default=3),
            ),
        )
        for block in blocks
    ]


def _merge_chunk_metadata(
    blocks: list[dict[str, str]], metadata_items: list[dict]
) -> list[dict[str, str]]:
    pending: dict[str, list[dict]] = {}
    for item in metadata_items:
        time_key = str(item.get("time", "")).strip()
        if time_key:
            pending.setdefault(time_key, []).append(item)

    merged: list[dict[str, str]] = []
    for block in blocks:
        time_key = extract_event_field(block["content"], "时间")
        item = pending.get(time_key, []).pop(0) if pending.get(time_key) else None
        keywords_str = (
            " ".join(item.get("keywords", [])) if item and isinstance(item.get("keywords"), list)
            else str(item.get("keywords", "")).strip() if item
            else extract_event_field(block["content"], "关键词")
        )
        importance = (
            int(item.get("importance", 3)) if item else parse_event_importance(block["content"], default=3)
        )
        merged.append(
            make_block(
                block["date"],
                _apply_chunk_metadata(block["content"], keywords_str, importance),
            )
        )
    return merged


def _validate_memory_merge_output(
    events: list[MemoryMergeEvent],
    window_dates: list[str],
) -> str | None:
    """验证 memory merge 结构化输出的完整性与日期范围。"""
    if not events:
        return "memory merge 输出事件列表为空"

    for event in events:
        if not all([event.date, event.time, event.location, event.participants, event.content]):
            return f"{event.date} 输出块结构不完整（存在空字段）"

    output_dates = {event.date for event in events}

    if window_dates:
        missing = [d for d in window_dates if d not in output_dates]
        if missing:
            preview = "、".join(missing[:5])
            suffix = f" 等{len(missing)}天" if len(missing) > 5 else ""
            return f"输出缺少日期：{preview}{suffix}"

        unexpected = [d for d in output_dates if d not in window_dates]
        if unexpected:
            preview = "、".join(unexpected[:5])
            suffix = f" 等{len(unexpected)}天" if len(unexpected) > 5 else ""
            return f"输出包含窗口外日期：{preview}{suffix}"

    return None



def _load_memory_blocks(agent_name: str) -> tuple[Path, str, list[dict[str, str]]] | None:
    path = Path(character_path(agent_name, "memory.md"))
    if not path.exists():
        return None
    original_content = path.read_text(encoding="utf-8")
    if len(original_content.strip()) < 50:
        return None
    sections = split_by_date(normalize(original_content))
    if not sections:
        return None
    return path, original_content, flatten_sections(sections)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class _ConsolidationWindow:
    stable_blocks: list[dict[str, str]]
    window_blocks: list[dict[str, str]]
    window_dates: list[str]
    window_memory_entries: str
    raw_dialogue: str


@dataclass
class _ConsolidationResult:
    agent_name: str
    days: int = 0
    date_range: str = ""
    original_len: int = 0
    final_len: int = 0
    user_md_before: int = 0
    user_md_after: int = 0
    skipped: bool = False
    skip_reason: str = ""
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# MemoryConsolidationFlow
# ---------------------------------------------------------------------------


class MemoryConsolidationFlow:
    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _supports_growth(agent_name: str) -> bool:
        return agent_name != "narrator"

    def _get_lock(self, name: str) -> asyncio.Lock:
        if name not in self._locks:
            self._locks[name] = asyncio.Lock()
        return self._locks[name]

    async def _run_consolidation_agent(
        self,
        *,
        agent,
        output_type: type,
        agent_name: str,
        function_name: str,
        user: str,
    ):
        return await run_structured_agent(
            agent=agent,
            user_input=user,
            output_type=output_type,
            timeout_seconds=120.0,
            workflow_name="agentgal_consolidation",
            trace_metadata={"agent_name": agent_name, "function": function_name},
            usage_agent=agent_name,
            usage_phase=f"consolidation.{function_name}",
            model_name=get_consolidation_llm_config(temperature=CONSOLIDATION_TEMPERATURE)["model"],
            error_label=f"{agent_name}.{function_name}",
        )

    async def _merge_memory_blocks(
        self,
        agent_name: str,
        memory_entries: str,
        raw_dialogue: str,
        window_dates: list[str],
    ) -> tuple[list[dict[str, str]], str]:
        user = build_memory_merge_payload(agent_name, memory_entries, raw_dialogue)
        output = await self._run_consolidation_agent(
            agent=get_memory_merge_agent(),
            output_type=MemoryMergeOutput,
            agent_name=agent_name,
            function_name="memory_merge",
            user=user,
        )

        if not output.events:
            raise ValueError("memory merge 返回事件列表为空")

        validation_error = _validate_memory_merge_output(output.events, window_dates)
        if validation_error:
            raise ValueError(validation_error)

        blocks = [
            make_block(
                event.date,
                (
                    f"- **时间**：{event.time}\n"
                    f"- **地点**：{event.location}\n"
                    f"- **在场**：{event.participants}\n"
                    f"- **内容**：{event.content}"
                ),
            )
            for event in output.events
        ]
        merged_markdown = render_sections(group_blocks(blocks))
        return blocks, merged_markdown

    async def _annotate_memory_metadata(
        self,
        agent_name: str,
        blocks: list[dict[str, str]],
        merged_markdown: str,
    ) -> list[dict[str, str]]:
        user = f"<consolidated_memory>\n{merged_markdown}\n</consolidated_memory>"
        try:
            output = await self._run_consolidation_agent(
                agent=get_memory_metadata_agent(),
                output_type=MemoryMetadataOutput,
                agent_name=agent_name,
                function_name="memory_metadata",
                user=user,
            )
            metadata_items = [
                {
                    "time": item.time,
                    "keywords": item.keywords,
                    "importance": item.importance,
                }
                for item in output.items
            ]
            return _merge_chunk_metadata(blocks, metadata_items)
        except Exception as e:
            routing_logger.error(f"[整理器] {agent_name} memory metadata 失败: {e}")
            return _apply_default_chunk_metadata(blocks)

    async def _extract_growth_updates(
        self,
        agent_name: str,
        merged_markdown: str,
    ) -> None:
        soul_content = read_agent_file(agent_name, "soul.md")
        growth_content = read_agent_file(agent_name, "growth.md") or "（尚无）"
        user = (
            f"<soul>\n{soul_content}\n</soul>\n\n"
            f"<existing_growth>\n{growth_content}\n</existing_growth>\n\n"
            f"<consolidated_memory>\n{merged_markdown}\n</consolidated_memory>"
        )
        output = await self._run_consolidation_agent(
            agent=get_growth_extract_agent(),
            output_type=GrowthExtractOutput,
            agent_name=agent_name,
            function_name="growth_extract",
            user=user,
        )
        updates = [{"content": entry.strip()} for entry in output.updates if entry.strip()]
        if updates:
            routing_logger.info(
                f"[整理器] {agent_name} growth.md: {self._apply_growth_updates(agent_name, updates)}"
            )
        else:
            routing_logger.info(f"[整理器] {agent_name} 无人格沉淀更新")

    async def _dedup_growth_entries(self, agent_name: str) -> None:
        current_count = len(read_growth_entries(agent_name))
        if current_count <= GROWTH_DEDUP_THRESHOLD:
            routing_logger.info(
                f"[整理器] {agent_name} 跳过 growth dedup（当前 {current_count} 条，未超过阈值 {GROWTH_DEDUP_THRESHOLD}）"
            )
            return
        routing_logger.info(
            f"[整理器] {agent_name} 触发 growth dedup（当前 {current_count} 条，阈值 {GROWTH_DEDUP_THRESHOLD}）"
        )
        growth_content = read_agent_file(agent_name, "growth.md") or "（尚无）"
        user = f"<existing_growth>\n{growth_content}\n</existing_growth>"
        output = await self._run_consolidation_agent(
            agent=get_growth_dedup_agent(),
            output_type=GrowthDedupOutput,
            agent_name=agent_name,
            function_name="growth_dedup",
            user=user,
        )
        entries = [entry.strip() for entry in output.entries if entry.strip()]
        if not entries:
            routing_logger.warning(f"[整理器] {agent_name} growth dedup 结果为空，跳过")
            return

        growth_path = Path(character_path(agent_name, "growth.md"))
        if growth_path.exists():
            backup_file(growth_path, agent_name, "growth")

        write_growth_entries(
            agent_name,
            {f"P{i:03d}": entry for i, entry in enumerate(entries, start=1)},
        )
        routing_logger.info(f"[整理器] {agent_name} growth dedup 完成，条目数: {len(entries)}")

    def _apply_growth_updates(self, agent_name: str, updates: list[dict]) -> str:
        entries = read_growth_entries(agent_name)
        logs: list[str] = []
        next_index = max(
            (int(match.group(1)) for key in entries if (match := re.fullmatch(r"P(\d{3})", key))),
            default=0,
        ) + 1
        for update in updates:
            content = (update.get("content") or "").strip()
            if not content:
                continue
            new_id = f"P{next_index:03d}"
            entries[new_id] = content
            logs.append(f"ADD {new_id}")
            next_index += 1
        write_growth_entries(agent_name, entries)
        return ";".join(logs) if logs else "无更新"

    async def _apply_consolidation_pipeline(
        self,
        agent_name: str,
        memory_entries: str,
        window_dates: list[str],
        raw_dialogue: str = "",
    ) -> tuple[list[dict[str, str]] | None, list[str]]:
        errors: list[str] = []

        try:
            blocks, merged_markdown = await self._merge_memory_blocks(
                agent_name, memory_entries, raw_dialogue, window_dates
            )
        except Exception as e:
            errors.append(f"第一步调用失败: {e}")
            routing_logger.error(f"[整理器] {agent_name} 第一步调用失败: {e}")
            return None, errors

        blocks = await self._annotate_memory_metadata(agent_name, blocks, merged_markdown)

        if not self._supports_growth(agent_name):
            routing_logger.info(f"[整理器] {agent_name} 跳过 growth.md 流程")
            return blocks, errors

        try:
            await self._extract_growth_updates(agent_name, merged_markdown)
        except Exception as e:
            errors.append(f"第二步调用失败: {e}")
            routing_logger.error(f"[整理器] {agent_name} 第二步调用失败: {e}")
            return blocks, errors

        try:
            await self._dedup_growth_entries(agent_name)
        except Exception as e:
            errors.append(f"第三步调用失败: {e}")
            routing_logger.error(f"[整理器] {agent_name} 第三步调用失败: {e}")

        return blocks, errors

    def _prepare_consolidation_window(
        self,
        agent_name: str,
        blocks: list[dict[str, str]],
    ) -> tuple[_ConsolidationWindow | None, str | None]:
        if not blocks:
            return None, "memory 中没有可整理的块"

        first_unstructured_index = next(
            (
                i
                for i, block in enumerate(blocks)
                if not is_structured_memory_block(block["content"])
            ),
            None,
        )
        start_index = (
            first_unstructured_index if first_unstructured_index is not None else len(blocks) - 1
        )

        stable_blocks = blocks[:start_index]
        window_blocks = blocks[start_index:]
        window_dates = list(OrderedDict.fromkeys(block["date"] for block in window_blocks))
        raw_dialogue = format_raw_dialogue_for_owner(agent_name, HISTORY_HIGH)
        if not raw_dialogue:
            return None, f"最近 {HISTORY_HIGH} 条消息中无参与"
        return _ConsolidationWindow(
            stable_blocks=stable_blocks,
            window_blocks=window_blocks,
            window_dates=window_dates,
            window_memory_entries=render_sections(group_blocks(window_blocks)),
            raw_dialogue=raw_dialogue,
        ), None

    async def _call_player_profile_agent(
        self,
        agent_name: str,
        draft_content: str,
    ) -> str:
        fields_def = build_fields_definition(agent_name)
        user_message = (
            f"<fields_definition>\n{fields_def}\n</fields_definition>\n\n"
            f"{_build_player_profile_draft_input(draft_content)}"
        )
        return await run_text_agent(
            agent=get_player_profile_agent(),
            user_input=user_message,
            timeout_seconds=120.0,
            workflow_name="agentgal_consolidation",
            trace_metadata={"agent_name": agent_name, "function": "player_profile"},
            usage_agent=agent_name,
            usage_phase="consolidation.player_profile",
            model_name=get_consolidation_llm_config(temperature=CONSOLIDATION_TEMPERATURE)["model"],
            error_label=f"{agent_name}.player_profile",
        )

    async def _consolidate_player_profile(self, agent_name: str) -> tuple[int, int]:
        user_path = Path(character_path(agent_name, "user.md"))
        tmp_path = Path(character_path(agent_name, "tmp_user.md"))

        if not user_path.exists():
            return 0, 0

        user_content = user_path.read_text(encoding="utf-8")
        tmp_content = tmp_path.read_text(encoding="utf-8").strip() if tmp_path.exists() else ""

        if not tmp_content:
            return 0, 0

        try:
            backup_file(user_path, agent_name, "user")
            consolidated = await self._call_player_profile_agent(agent_name, tmp_content)
            consolidated = _enforce_user_section_limits(consolidated)

            if len(consolidated.strip()) < 20:
                routing_logger.warning(f"[整理器] {agent_name} user.md LLM 返回过短，跳过")
                return 0, 0

            user_path.write_text(consolidated.strip() + "\n", encoding="utf-8")
            tmp_path.unlink(missing_ok=True)
            before_len, after_len = len(user_content), len(consolidated)
            routing_logger.info(
                f"[整理器] {agent_name} user.md 整理完成 (长度: {before_len} → {after_len})"
            )
            return before_len, after_len
        except Exception as e:
            routing_logger.error(f"[整理器] {agent_name} user.md 整理失败: {e}")
            return 0, 0

    async def consolidate_agent(self, agent_name: str) -> _ConsolidationResult | None:
        result = _ConsolidationResult(agent_name=agent_name)
        if agent_name == "narrator":
            result.skipped, result.skip_reason = True, "旁白不维护 memory.md，也不参与整理"
            return result

        lock = self._get_lock(agent_name)
        if lock.locked():
            result.skipped, result.skip_reason = True, "已有整理任务在运行"
            return result

        async with lock:
            loaded = _load_memory_blocks(agent_name)
            if loaded is None:
                return None
            path, original_content, blocks = loaded
            window, skip_reason = self._prepare_consolidation_window(agent_name, blocks)
            if window is None:
                if skip_reason:
                    result.skipped, result.skip_reason = True, skip_reason
                    routing_logger.info(f"[整理器] {agent_name} 跳过: {skip_reason}")
                return result if result.skipped else None

            result.days = len(window.window_dates)
            result.date_range = f"{window.window_dates[0]}~{window.window_dates[-1]}"
            result.original_len = len(original_content)
            backup_file(path, agent_name, "Memory")

            merged_blocks = window.stable_blocks + window.window_blocks
            try:
                rewritten_blocks, pipeline_errors = await self._apply_consolidation_pipeline(
                    agent_name,
                    window.window_memory_entries,
                    window.window_dates,
                    window.raw_dialogue,
                )
                result.errors.extend(pipeline_errors)
                if rewritten_blocks is not None:
                    merged_blocks = window.stable_blocks + rewritten_blocks
            except Exception as e:
                result.errors.append(f"整合失败: {e}")
                routing_logger.error(f"[整理器] {agent_name} 整合失败: {e}")

            merged_sections = group_blocks(merged_blocks)
            result.final_len, _consolidated_len = safe_write_memory(
                path, merged_sections, agent_name, original_content
            )
            if result.final_len < 0:
                result.errors.append("并发冲突：检测到中间变更，已放弃写回")
                return result

            actual_window_blocks = merged_blocks[len(window.stable_blocks):]
            update_dates = set(window.window_dates) | {
                block["date"] for block in actual_window_blocks
            }
            for date in update_dates:
                events = split_into_events(merged_sections.get(date, ""))
                chunks = [
                    (text, extract_event_field(text, "关键词"), parse_event_importance(text, default=3))
                    for text in events
                ]
                await vector_store.add(agent_name, date, chunks)

            user_before, user_after = await self._consolidate_player_profile(agent_name)
            result.user_md_before, result.user_md_after = user_before, user_after
            return result

    async def run_memory_merge_for_date(
        self,
        agent_name: str,
        date: str,
        sections: dict,
    ) -> tuple[str | None, str]:
        memory_entries = sections.get(date, "")
        if not memory_entries:
            return None, f"没有找到 {date} 的记忆"
        raw_dialogue = format_raw_dialogue_for_owner(agent_name, HISTORY_HIGH)
        try:
            blocks, _ = await self._merge_memory_blocks(agent_name, memory_entries, raw_dialogue, [date])
            date_blocks = [block for block in blocks if block["date"] == date]
            if not date_blocks:
                return None, "memory merge 结果中未找到目标日期"
            content = "\n\n".join(block["content"] for block in date_blocks)
            return content, f"整理完成，共 {len(date_blocks)} 个事件块"
        except Exception as e:
            return None, f"整理失败: {e}"

    async def run_player_profile_consolidation(
        self,
        agent_name: str,
        draft_content: str,
    ) -> str | None:
        try:
            return await self._call_player_profile_agent(agent_name, draft_content)
        except Exception as e:
            routing_logger.error(f"[整理器] {agent_name} user.md 整理失败: {e}")
            return None

    async def consolidate_all(self, agent_names: list[str]):
        t0 = time.monotonic()
        agent_names = [name for name in agent_names if name != "narrator"]
        if not agent_names:
            routing_logger.info("[整理器] 无需整理：当前列表中没有可整理角色")
            return

        summaries: list[str] = []
        for name in agent_names:
            path = Path(character_path(name, "memory.md"))
            summaries.append(
                f"{name}.memory({len(path.read_text(encoding='utf-8'))}字)"
                if path.exists()
                else f"{name}(无文件)"
            )
        routing_logger.info(f"[整理器] 开始记忆整理: {', '.join(summaries)}")

        raw_results = await asyncio.gather(
            *(self.consolidate_agent(name) for name in agent_names),
            return_exceptions=True,
        )
        for item in raw_results:
            if isinstance(item, Exception):
                routing_logger.error(f"[整理器] 异常: {item}")
                continue
            if item is None:
                continue
            if item.skipped:
                routing_logger.info(f"[整理器] {item.agent_name} 跳过: {item.skip_reason}")
                continue

            mem_part = (
                f"{item.original_len}→{item.final_len}字"
                f"({(item.final_len - item.original_len) / item.original_len * 100:+.1f}%)"
                if item.original_len > 0
                else "无变化"
            )
            user_part = (
                f" | user.md {item.user_md_before}→{item.user_md_after}"
                if item.user_md_before > 0
                else ""
            )
            err_part = f" | 错误: {', '.join(item.errors)}" if item.errors else ""
            routing_logger.info(
                f"[整理器] {item.agent_name} 完成: {item.days}天({item.date_range}) "
                f"{mem_part}{user_part}{err_part}"
            )

        routing_logger.info(f"[整理器] 全部完成 (耗时 {time.monotonic() - t0:.1f}s)")


memory_consolidation_flow = MemoryConsolidationFlow()
