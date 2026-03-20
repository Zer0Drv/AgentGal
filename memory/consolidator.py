"""后台记忆整理器。

memory.md 会先按日期标准化，再按事件块展开。
整理时只重写尾部窗口：从最后一个已整理块开始，到当前文件末尾结束。
"""

import asyncio
import hashlib
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from llm.llm_parser import OpenAICompatibleClient
from llm.providers import get_consolidation_llm_config

from log_config.memory import memory_logger as routing_logger
from log_config.consolidation_calls import log_consolidation_call
from engine.config import (
    CONSOLIDATION_MAX_TOKENS,
    CONSOLIDATION_TEMPERATURE,
    GROWTH_DEDUP_THRESHOLD,
    RAW_DIALOGUE_LIMIT,
    character_path,
)
from memory.consolidation_inputs import (
    build_step1_user_payload,
    format_raw_dialogue_for_owner,
)
from memory.file_ops import (
    _get_fields_from_file,
    backup_file,
    load_text,
    normalize,
    read_growth_entries,
    read_agent_file,
    read_sidecar_json,
    safe_write_memory,
    split_by_date,
    split_into_events,
    write_growth_entries,
    write_sidecar_json,
)
from memory.vector_store import vector_store

_PROMPT_STEP1_PATH = Path(__file__).parent.parent / "prompts" / "memory_scene_merge.txt"
_PROMPT_STEP2_PATH = Path(__file__).parent.parent / "prompts" / "growth_extract.txt"
_PROMPT_STEP3_PATH = Path(__file__).parent.parent / "prompts" / "growth_dedupe.txt"
_PLAYER_PROMPT_PATH = (
    Path(__file__).parent.parent / "prompts" / "player_profile_consolidation_prompt.txt"
)


# 字段描述映射（用于 user.md 整理）
_USER_FIELD_DESCRIPTIONS: dict[str, str] = {
    "基本信息": "优先保留已确认的客观信息：姓名、年龄、性别、身份",
    "他是什么人": "最多 8 条：跨情境成立的性格、习惯、边界方式与行事风格（主语是\"他\"），不要重复基本信息",
    "我们怎么相处": "最多 5 条：我和他之间反复出现的双向互动规律（主语是\"我们/我和他\"）",
}


def build_fields_definition(agent_name: str) -> str:
    """根据当前使用的文件生成 user.md 字段定义文本

    从 data/characters/{agent}/user.md 读取字段结构，
    不存在则使用默认字段。
    """
    file_path = character_path(agent_name, "user.md")
    fields = _get_fields_from_file(file_path)
    if fields is None:
        fields = ["基本信息", "他是什么人", "我们怎么相处"]

    lines = []
    for field in fields:
        desc = _USER_FIELD_DESCRIPTIONS.get(field, "")
        lines.append(f"- 「{field}」：{desc}")
    return "\n".join(lines)


@dataclass
class _ConsolidationResult:
    """单个 agent 整理结果，用于汇总日志"""

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


@dataclass(frozen=True)
class _MemoryBlock:
    date: str
    content: str
    marker: str


class MemoryConsolidator:
    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _supports_growth(agent_name: str) -> bool:
        """仅角色维护 growth.md，narrator 不使用。"""
        return agent_name != "narrator"

    def _get_lock(self, name: str) -> asyncio.Lock:
        """获取按 agent 名称隔离的整理锁。"""
        if name not in self._locks:
            self._locks[name] = asyncio.Lock()
        return self._locks[name]

    def _load_and_normalize(
        self, agent_name: str
    ) -> tuple[Path, str, OrderedDict[str, str]] | None:
        """
        加载 memory.md，normalize 并按日期分割。

        Returns:
            (文件路径, 原始内容, sections) 或 None（文件不存在或太短）
        """
        path = Path(character_path(agent_name, "memory.md"))
        if not path.exists():
            return None
        original = path.read_text(encoding="utf-8")
        if len(original.strip()) < 50:
            return None

        content = normalize(original)
        sections = split_by_date(content)
        if not sections:
            return None

        return path, original, sections

    def _build_consolidation_prompt_step2(
        self, agent_name: str, step1_result: str
    ) -> tuple[str, str]:
        """构建第二步 prompt：成长事件判断（soul/growth/input 放 user message）。

        Returns:
            (system, user): system 为纯指令，user 为结构化数据 + 第一步整理结果
        """
        soul_content = read_agent_file(agent_name, "soul.md")
        growth_content = read_agent_file(agent_name, "growth.md") or "（尚无）"

        system = load_text(_PROMPT_STEP2_PATH)
        user = (
            f"<soul>\n{soul_content}\n</soul>\n\n"
            f"<existing_growth>\n{growth_content}\n</existing_growth>\n\n"
            f"<consolidated_memory>\n{step1_result}\n</consolidated_memory>"
        )
        return system, user

    def _build_consolidation_prompt_step3(self, agent_name: str) -> tuple[str, str]:
        """构建第三步 prompt：growth.md 超限合并压缩（growth 放 user message）。

        Returns:
            (system, user): system 为纯指令，user 为待合并的 growth 数据
        """
        growth_content = read_agent_file(agent_name, "growth.md") or "（尚无）"
        system = load_text(_PROMPT_STEP3_PATH)
        user = f"<existing_growth>\n{growth_content}\n</existing_growth>"
        return system, user

    def _build_block_marker(self, date: str, content: str) -> str:
        """为单个记忆块生成稳定标识。"""
        normalized_lines = [line.rstrip() for line in content.strip().splitlines()]
        normalized = "\n".join(normalized_lines).strip()
        return hashlib.sha1(f"{date}\n{normalized}".encode("utf-8")).hexdigest()

    def _flatten_memory_sections(
        self,
        sections: OrderedDict[str, str],
    ) -> list[_MemoryBlock]:
        """把按日期组织的内容转换成有序记忆块列表。"""
        blocks: list[_MemoryBlock] = []
        for date, body in sections.items():
            for content in split_into_events(body):
                stripped = content.strip()
                if not stripped:
                    continue
                blocks.append(
                    _MemoryBlock(
                        date=date,
                        content=stripped,
                        marker=self._build_block_marker(date, stripped),
                    )
                )
        return blocks

    def _group_memory_blocks(
        self,
        blocks: list[_MemoryBlock],
    ) -> OrderedDict[str, str]:
        """把记忆块列表重新组装成按日期组织的内容。"""
        sections: OrderedDict[str, str] = OrderedDict()
        for block in blocks:
            sections[block.date] = (
                sections.get(block.date, "")
                + ("\n\n" if block.date in sections else "")
                + block.content.strip()
            )
        return sections

    def _render_memory_sections(self, sections: OrderedDict[str, str]) -> str:
        """把按日期组织的内容渲染成 markdown。"""
        parts: list[str] = []
        for date, body in sections.items():
            parts.append(f"## {date}")
            parts.append(body.strip())
            parts.append("")
        return "\n".join(parts).strip()

    def _resolve_anchor_marker(
        self,
        agent_name: str,
        blocks: list[_MemoryBlock],
        original_content: str,
        cdata: dict,
    ) -> tuple[str | None, bool]:
        """根据状态恢复尾部窗口的起点块标识。"""
        stored_marker = cdata.get("last_consolidated_block_id")
        if stored_marker:
            if any(block.marker == stored_marker for block in blocks):
                return stored_marker, False
            routing_logger.warning(
                "[整理器] %s 已整理块标识失效，尝试用 last_memory_size 恢复边界",
                agent_name,
            )

        last_memory_size = cdata.get("last_memory_size")
        if isinstance(last_memory_size, int) and 0 < last_memory_size <= len(original_content):
            snapshot_content = normalize(original_content[:last_memory_size])
            snapshot_sections = split_by_date(snapshot_content)
            snapshot_blocks = self._flatten_memory_sections(snapshot_sections)
            if snapshot_blocks:
                snapshot_marker = snapshot_blocks[-1].marker
                if any(block.marker == snapshot_marker for block in blocks):
                    if stored_marker and stored_marker != snapshot_marker:
                        routing_logger.info(
                            "[整理器] %s 用 last_memory_size 恢复已整理块边界: %s -> %s",
                            agent_name,
                            stored_marker[:10],
                            snapshot_marker[:10],
                        )
                    return snapshot_marker, False
                return None, True

        if cdata:
            routing_logger.warning("[整理器] %s 缺少可用的块边界状态", agent_name)
            return None, True

        return None, False

    def _resolve_tail_start(
        self,
        agent_name: str,
        blocks: list[_MemoryBlock],
        original_content: str,
        cdata: dict,
    ) -> tuple[int | None, str | None]:
        """根据当前状态计算尾部整理窗口起点。"""
        if not blocks:
            return None, "memory 中没有可整理的块"

        anchor_marker, boundary_invalid = self._resolve_anchor_marker(
            agent_name, blocks, original_content, cdata
        )
        stored_marker = cdata.get("last_consolidated_block_id")
        migrated_anchor = bool(
            stored_marker
            and anchor_marker
            and stored_marker != anchor_marker
            and not any(block.marker == stored_marker for block in blocks)
        )
        last_memory_size = cdata.get("last_memory_size")
        has_growth = not isinstance(last_memory_size, int) or len(original_content) > last_memory_size

        start_index = 0
        if anchor_marker:
            start_index = next(
                i for i, block in enumerate(blocks) if block.marker == anchor_marker
            )
            if (
                not migrated_anchor
                and isinstance(last_memory_size, int)
                and len(original_content) <= last_memory_size
            ):
                return None, "memory 未增长，跳过本轮整理"
            return start_index, None

        if boundary_invalid:
            if not has_growth:
                return None, "memory 未增长，跳过本轮整理"
            start_index = max(len(blocks) - 2, 0)
            routing_logger.warning(
                "[整理器] %s 已整理块边界失效，回退到最近 %s 块",
                agent_name,
                len(blocks) - start_index,
            )
        return start_index, None

    def _apply_step3_growth(self, agent_name: str, llm_result: str) -> None:
        """解析第三步输出并整体覆写 growth.md。"""
        match = re.search(r"<merged_growth>(.*?)</merged_growth>", llm_result, re.DOTALL)
        if not match:
            routing_logger.warning(f"[整理器] {agent_name} 第三步未找到 <merged_growth> 标签，跳过")
            return

        raw = match.group(1).strip()
        # 解析每行 [Pxxx] [日期] 内容
        entries: dict[str, str] = {}
        for line in raw.splitlines():
            line = line.strip()
            m = re.match(r"\[(P\d+)\]\s*(.*)", line)
            if m:
                entries[m.group(1)] = m.group(2).strip()

        if not entries:
            routing_logger.warning(f"[整理器] {agent_name} 第三步解析结果为空，跳过")
            return

        # 备份 growth.md
        growth_path = Path(character_path(agent_name, "growth.md"))
        if growth_path.exists():
            backup_file(growth_path, agent_name, "growth")

        # 重新从 P001 顺序编号，消除合并后的空缺
        reindexed = {
            f"P{i:03d}": content
            for i, content in enumerate(entries.values(), start=1)
        }
        write_growth_entries(agent_name, reindexed)
        routing_logger.info(
            f"[整理器] {agent_name} 第三步合并完成，条目数: {len(entries)}"
        )

    async def _run_memory_pipeline(
        self,
        agent_name: str,
        memory_entries: str,
        window_dates: list[str],
        raw_dialogue: str = "",
    ) -> tuple[list[_MemoryBlock] | None, list[str]]:
        """执行三步 LLM 整理 pipeline，返回新的尾部块与错误列表。"""
        errors: list[str] = []
        rewritten_blocks: list[_MemoryBlock] | None = None

        async with OpenAICompatibleClient(
            **get_consolidation_llm_config(temperature=CONSOLIDATION_TEMPERATURE),
            max_tokens=CONSOLIDATION_MAX_TOKENS, timeout=120.0, max_retries=3,
        ) as client:
            # ===== 第一步：调用 LLM 进行归并整理 =====
            system_step1 = load_text(_PROMPT_STEP1_PATH)
            user_step1 = build_step1_user_payload(agent_name, memory_entries, raw_dialogue)
            try:
                resp1 = await client.chat(
                    [{"role": "system", "content": system_step1}, {"role": "user", "content": user_step1}],
                    enable_thinking=False,
                )
                step1_result = (resp1.get("content") or "").strip()
                log_consolidation_call(
                    agent_name, "step1_merge",
                    f"[system]\n{system_step1}\n\n[user]\n{user_step1}",
                    step1_result, resp1.get("usage"),
                )
            except Exception as e:
                errors.append(f"第一步调用失败: {e}")
                routing_logger.error(f"[整理器] {agent_name} 第一步调用失败: {e}")
                return None, errors

            if len(step1_result) < 50:
                errors.append("第一步返回过短，跳过整理")
                return None, errors

            step1_sections = self._parse_step1_memories(step1_result, window_dates)
            date_error = self._validate_step1_dates(window_dates, step1_sections)
            if date_error:
                errors.append(date_error)
                return None, errors

            rewritten_blocks = self._flatten_memory_sections(step1_sections)
            if not rewritten_blocks:
                errors.append("未能解析第一步:归并整理")
                return None, errors

            for block in rewritten_blocks:
                missing = self._check_event_fields(block.content)
                if missing:
                    routing_logger.warning(
                        "[整理器] %s 整理结果存在字段缺失 %s，保留原始窗口",
                        agent_name,
                        missing,
                    )
                    errors.append(f"{block.date} 字段不完整({','.join(missing)})，已跳过")
                    return None, errors

            step1_markdown = self._render_memory_sections(
                self._group_memory_blocks(rewritten_blocks)
            )

            if not self._supports_growth(agent_name):
                routing_logger.info(f"[整理器] {agent_name} 跳过 growth.md 流程")
                return rewritten_blocks, errors

            # ===== 第二步：调用 LLM 进行成长事件判断 =====
            system_step2, user_step2 = self._build_consolidation_prompt_step2(agent_name, step1_markdown)
            try:
                resp2 = await client.chat(
                    [{"role": "system", "content": system_step2}, {"role": "user", "content": user_step2}],
                    enable_thinking=False,
                )
                step2_result = (resp2.get("content") or "").strip()
                log_consolidation_call(
                    agent_name, "step2_growth",
                    f"[system]\n{system_step2}\n\n[user]\n{user_step2}",
                    step2_result, resp2.get("usage"),
                )
            except Exception as e:
                errors.append(f"第二步调用失败: {e}")
                routing_logger.error(f"[整理器] {agent_name} 第二步调用失败: {e}")
                return rewritten_blocks, errors

            # 解析第二步结果并更新 growth.md
            step2_updates = self._parse_step2_growth(step2_result)
            if step2_updates:
                routing_logger.info(f"[整理器] {agent_name} growth.md: {self._apply_growth_updates(agent_name, step2_updates)}")
            else:
                routing_logger.info(f"[整理器] {agent_name} 无人格沉淀更新")

            # ===== 第三步：超过阈值时再去重合并 growth.md =====
            current_count = len(read_growth_entries(agent_name))
            if current_count > GROWTH_DEDUP_THRESHOLD:
                routing_logger.info(
                    f"[整理器] {agent_name} 触发第三步去重合并（当前 {current_count} 条，阈值 {GROWTH_DEDUP_THRESHOLD}）"
                )
                system_step3, user_step3 = self._build_consolidation_prompt_step3(agent_name)
                try:
                    resp3 = await client.chat(
                        [{"role": "system", "content": system_step3}, {"role": "user", "content": user_step3}],
                        enable_thinking=False,
                    )
                    step3_result = (resp3.get("content") or "").strip()
                    log_consolidation_call(
                        agent_name, "step3_dedup",
                        f"[system]\n{system_step3}\n\n[user]\n{user_step3}",
                        step3_result, resp3.get("usage"),
                    )
                    self._apply_step3_growth(agent_name, step3_result)
                except Exception as e:
                    errors.append(f"第三步调用失败: {e}")
                    routing_logger.error(f"[整理器] {agent_name} 第三步调用失败: {e}")
            else:
                routing_logger.info(
                    f"[整理器] {agent_name} 跳过第三步去重合并（当前 {current_count} 条，未超过阈值 {GROWTH_DEDUP_THRESHOLD}）"
                )

        return rewritten_blocks, errors

    async def consolidate_agent(
        self, agent_name: str
    ) -> "_ConsolidationResult | None":
        """整理单个 agent 的记忆，返回结果摘要（供 consolidate_all 汇总日志）"""
        result = _ConsolidationResult(agent_name=agent_name)
        if agent_name == "narrator":
            result.skipped, result.skip_reason = True, "旁白不维护 memory.md，也不参与整理"
            return result

        lock = self._get_lock(agent_name)
        if lock.locked():
            result.skipped, result.skip_reason = True, "已有整理任务在运行"
            return result

        async with lock:
            # 1. 加载文件并 normalize
            loaded = self._load_and_normalize(agent_name)
            if loaded is None:
                return None
            path, original_content, sections = loaded
            blocks = self._flatten_memory_sections(sections)
            cdata = read_sidecar_json(agent_name, ".consolidation_state.json")
            start_index, window_skip_reason = self._resolve_tail_start(
                agent_name, blocks, original_content, cdata
            )
            if start_index is None:
                if window_skip_reason:
                    result.skipped, result.skip_reason = True, window_skip_reason
                    routing_logger.info(f"[整理器] {agent_name} 跳过: {window_skip_reason}")
                return result if result.skipped else None

            stable_blocks = blocks[:start_index]
            window_blocks = blocks[start_index:]
            window_dates = list(OrderedDict.fromkeys(block.date for block in window_blocks))
            window_sections = self._group_memory_blocks(window_blocks)
            window_memory_entries = self._render_memory_sections(window_sections)

            # 3. guard 检查：近期无参与 / 文件增长不足
            raw_dialogue = format_raw_dialogue_for_owner(agent_name, RAW_DIALOGUE_LIMIT)
            if not raw_dialogue:
                result.skipped, result.skip_reason = True, f"最近 {RAW_DIALOGUE_LIMIT} 条消息中无参与"
                routing_logger.info(f"[整理器] {agent_name} 跳过: {result.skip_reason}")
                return result

            # 4. 备份 → LLM pipeline → 写回
            result.days = len(window_dates)
            result.date_range = f"{window_dates[0]}~{window_dates[-1]}"
            result.original_len = len(original_content)
            backup_file(path, agent_name, "Memory")

            merged_blocks = stable_blocks + window_blocks
            try:
                rewritten_blocks, pipeline_errors = await self._run_memory_pipeline(
                    agent_name,
                    window_memory_entries,
                    window_dates,
                    raw_dialogue,
                )
                result.errors.extend(pipeline_errors)
                if rewritten_blocks is not None:
                    merged_blocks = stable_blocks + rewritten_blocks
            except Exception as e:
                result.errors.append(f"整合失败: {e}")
                routing_logger.error(f"[整理器] {agent_name} 整合失败: {e}")

            merged_sections = self._group_memory_blocks(merged_blocks)
            result.final_len, consolidated_len = safe_write_memory(
                path, merged_sections, agent_name, original_content
            )
            if result.final_len < 0:
                result.errors.append("并发冲突：检测到中间变更，已放弃写回")
                return result

            # 5. 向量同步 + 进度更新
            for date in window_dates:
                await vector_store.add(
                    agent_name,
                    date,
                    split_into_events(merged_sections.get(date, "")),
                )

            if not result.errors:
                last_block = merged_blocks[-1]
                cdata.update(
                    last_consolidated_date=last_block.date,
                    last_consolidated_block_id=last_block.marker,
                    last_memory_size=consolidated_len,
                )
                write_sidecar_json(agent_name, ".consolidation_state.json", cdata)

            # 6. Player profile 整理（与 memory 整理正交）
            user_before, user_after = await self._consolidate_player_profile(agent_name)
            result.user_md_before, result.user_md_after = user_before, user_after

            return result

    # ===== 双解析器：第一步 + 第二步 =====

    _REQUIRED_EVENT_FIELDS = ["**时间**", "**地点**", "**在场**", "**内容**"]

    def _check_event_fields(self, section_content: str) -> list[str]:
        """检查一个日期的整理结果是否每个事件块都包含必要字段。

        Args:
            section_content: 某一天的整理后内容（可能含多个事件块）

        Returns:
            缺失的字段名列表，空列表表示完整
        """
        missing: list[str] = []
        for field in self._REQUIRED_EVENT_FIELDS:
            if field not in section_content:
                missing.append(field)
        return missing

    def _validate_step1_dates(
        self,
        expected_dates: list[str],
        step1_sections: OrderedDict[str, str],
    ) -> str | None:
        """校验 step1 是否完整返回了当前窗口覆盖的日期。"""
        actual_dates = list(step1_sections.keys())
        if actual_dates == expected_dates:
            return None

        expected = ", ".join(expected_dates) or "（空）"
        actual = ", ".join(actual_dates) or "（空）"
        routing_logger.warning(
            "[整理器] step1 返回日期与窗口不一致: expected=%s, actual=%s",
            expected,
            actual,
        )
        return f"第一步返回日期与窗口不一致(expected={expected}; actual={actual})"

    def _parse_step1_memories(
        self,
        llm_result: str,
        expected_dates: list[str] | None = None,
    ) -> OrderedDict[str, str]:
        """
        从第一步 LLM 输出中提取归并整理后的日记内容。

        兼容两类输出：
        1. 扁平列表：日期写在时间字段中
        - **时间**：4月3日 上午
        - **地点**：教室
        - **在场**：莉莉丝、李小明
        - **内容**：事件描述...

        2. 带日期标题：时间字段只写时段/时间
        ## 4月3日
        **时间**：上午
        **地点**：教室
        **在场**：莉莉丝、李小明
        **内容**：事件描述...

        Returns:
            OrderedDict[日期, 该日期的内容]
        """
        # 移除 analysis 标签及其内容（工作流第一步的分析部分）
        cleaned = re.sub(r"<analysis>.*?</analysis>", "", llm_result, flags=re.DOTALL)
        cleaned = cleaned.strip()

        heading_pattern = re.compile(r"^\s*##\s*(\d{1,2}月\d{1,2}日)(?:\s.*)?$")
        time_pattern = re.compile(r"^\s*(?:-\s*)?\*\*时间\*\*：\s*(.*)$")
        field_pattern = re.compile(r"^\s*(?:-\s*)?\*\*(地点|在场|内容)\*\*：\s*(.*)$")
        explicit_date_pattern = re.compile(r"(\d{1,2}月\d{1,2}日)")
        fallback_date = expected_dates[0] if expected_dates and len(expected_dates) == 1 else None

        sections: OrderedDict[str, str] = OrderedDict()
        current_heading_date: str | None = None
        current_date: str | None = None
        current_lines: list[str] = []

        def flush_event() -> None:
            nonlocal current_date, current_lines
            if not current_date or not current_lines:
                current_date = None
                current_lines = []
                return
            event_text = "\n".join(current_lines).strip()
            if event_text:
                sections[current_date] = (
                    sections.get(current_date, "")
                    + ("\n\n" if current_date in sections else "")
                    + event_text
                )
            current_date = None
            current_lines = []

        for line in cleaned.splitlines():
            heading_match = heading_pattern.match(line)
            if heading_match:
                current_heading_date = heading_match.group(1)
                continue

            time_match = time_pattern.match(line)
            if time_match:
                flush_event()
                time_value = time_match.group(1).strip()
                explicit_date_match = explicit_date_pattern.search(time_value)
                current_date = (
                    explicit_date_match.group(1)
                    if explicit_date_match
                    else current_heading_date or fallback_date
                )
                if explicit_date_match:
                    normalized_time = f"- **时间**：{time_value}"
                elif current_date:
                    normalized_time = f"- **时间**：{current_date} {time_value}".rstrip()
                else:
                    normalized_time = f"- **时间**：{time_value}"
                current_lines = [normalized_time]
                continue

            if current_lines:
                field_match = field_pattern.match(line)
                if field_match:
                    field_name = field_match.group(1)
                    field_value = field_match.group(2).strip()
                    current_lines.append(f"- **{field_name}**：{field_value}".rstrip())
                else:
                    current_lines.append(line)

        flush_event()
        return sections

    def _parse_step2_growth(self, llm_result: str) -> list[dict]:
        """
        从 LLM 输出中提取第二步：人格沉淀更新。

        Returns:
            [{"content": "..."}]
        """
        # 提取 personality_updates 部分
        pattern = r"<personality_updates>(.*?)</personality_updates>"
        match = re.search(pattern, llm_result, re.DOTALL)

        if not match:
            return []

        raw_updates = match.group(1).strip()
        if not raw_updates:
            return []

        updates = []
        # 兼容旧格式：<update>...</update>
        tag_pattern = r"<update\b[^>]*>(.*?)</update>"
        tagged_matches = list(re.finditer(tag_pattern, raw_updates, re.DOTALL))
        if tagged_matches:
            for m in tagged_matches:
                content = m.group(1).strip() if m.group(1) else ""
                if content:
                    updates.append({"content": content})
            return updates

        # 新格式：<personality_updates> 内一行一条
        for line in raw_updates.splitlines():
            content = line.strip()
            if content:
                updates.append({"content": content})

        return updates

    def _apply_growth_updates(self, agent_name: str, updates: list[dict]) -> str:
        """
        应用人格沉淀更新到 growth.md

        Returns:
            操作日志字符串
        """
        entries = read_growth_entries(agent_name)
        logs = []
        next_index = max(
            (int(m.group(1)) for key in entries if (m := re.fullmatch(r"P(\d{3})", key))),
            default=0,
        ) + 1

        for up in updates:
            content = (up.get("content") or "").strip()
            if not content:
                continue

            new_id = f"P{next_index:03d}"
            entries[new_id] = content
            logs.append(f"ADD {new_id}")
            next_index += 1

        write_growth_entries(agent_name, entries)
        return ";".join(logs) if logs else "无更新"

    async def _consolidate_player_profile(self, agent_name: str) -> tuple[int, int]:
        """整理单个角色的 user.md（去重精炼）。返回 (原始长度, 整理后长度)。"""
        user_path = Path(character_path(agent_name, "user.md"))
        if not user_path.exists():
            return 0, 0

        content = user_path.read_text(encoding="utf-8")

        # 内容太短不需要整理
        if len(content.strip()) < 100:
            return 0, 0

        try:
            # 备份
            backup_file(user_path, agent_name, "user")

            fields_def = build_fields_definition(agent_name)
            system = load_text(_PLAYER_PROMPT_PATH).format(fields_definition=fields_def)
            async with OpenAICompatibleClient(
                **get_consolidation_llm_config(temperature=CONSOLIDATION_TEMPERATURE),
                timeout=120.0, max_retries=3,
            ) as client:
                resp = await client.chat(
                    [{"role": "system", "content": system}, {"role": "user", "content": content}],
                    enable_thinking=False,
                )
            consolidated = (resp.get("content") or "").strip()

            if len(consolidated.strip()) < 20:
                routing_logger.warning(
                    f"[整理器] {agent_name} user.md LLM 返回过短，跳过"
                )
                return 0, 0

            # 两步式 prompt：提取「第二步：档案」之后的内容
            diary_match = re.search(
                r"^.*第二步.*档案.*$",
                consolidated,
                re.MULTILINE,
            )
            if diary_match:
                consolidated = consolidated[diary_match.end():].lstrip("\n")

            user_path.write_text(consolidated.strip() + "\n", encoding="utf-8")
            return len(content), len(consolidated)

        except Exception as e:
            routing_logger.error(f"[整理器] {agent_name} user.md 整理失败: {e}")
            return 0, 0

    # 保留公开方法名兼容外部调用（如 scripts/consolidate_memories.py）
    async def consolidate_player_profile(self, agent_name: str):
        """整理单个角色的 user.md（公开接口，带独立日志）"""
        before, after = await self._consolidate_player_profile(agent_name)
        if before > 0:
            routing_logger.info(
                f"[整理器] {agent_name} user.md 整理完成 (长度: {before} → {after})"
            )

    async def consolidate_all(self, agent_names: list[str]):
        t0 = time.monotonic()
        agent_names = [name for name in agent_names if name != "narrator"]
        if not agent_names:
            routing_logger.info("[整理器] 无需整理：当前列表中没有可整理角色")
            return

        # 收集各 agent 的摘要信息用于开始日志
        summaries: list[str] = []
        for name in agent_names:
            path = Path(character_path(name, "memory.md"))
            if path.exists():
                length = len(path.read_text(encoding="utf-8"))
                summaries.append(f"{name}.memory({length}字)")
            else:
                summaries.append(f"{name}(无文件)")

        routing_logger.info(f"[整理器] 开始记忆整理: {', '.join(summaries)}")

        raw_results = await asyncio.gather(
            *(self.consolidate_agent(n) for n in agent_names),
            return_exceptions=True,
        )

        # 汇总输出每个 agent 一行结果
        for r in raw_results:
            if isinstance(r, Exception):
                routing_logger.error(f"[整理器] 异常: {r}")
                continue
            if r is None:
                continue
            if r.skipped:
                routing_logger.info(f"[整理器] {r.agent_name} 跳过: {r.skip_reason}")
                continue

            if r.original_len > 0:
                ratio = (r.final_len - r.original_len) / r.original_len * 100
                mem_part = f"{r.original_len}→{r.final_len}字({ratio:+.1f}%)"
            else:
                mem_part = "无变化"

            user_part = ""
            if r.user_md_before > 0:
                user_part = f" | user.md {r.user_md_before}→{r.user_md_after}"

            err_part = ""
            if r.errors:
                err_part = f" | 错误: {', '.join(r.errors)}"

            routing_logger.info(
                f"[整理器] {r.agent_name} 完成: "
                f"{r.days}天({r.date_range}) {mem_part}{user_part}{err_part}"
            )

        elapsed = time.monotonic() - t0
        routing_logger.info(f"[整理器] 全部完成 (耗时 {elapsed:.1f}s)")


memory_consolidator = MemoryConsolidator()
