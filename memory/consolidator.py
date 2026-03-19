"""后台记忆整理器 - 每个日期都过 LLM 整合 + 增量进度

normalize → split_by_date（合并同日期段落）→ 根据进度跳过已整合日期

进度机制：记录上次整合到哪个日期，下次从该日期开始，避免过度压缩旧记忆。
"""

import asyncio
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
    CONSOLIDATION_SIZE_THRESHOLD,
    CONSOLIDATION_TEMPERATURE,
    GROWTH_DEDUP_THRESHOLD,
    RAW_DIALOGUE_LIMIT,
    character_path,
)
from game.save_manager import load_conversation_history
from memory.file_ops import (
    _get_fields_from_file,
    backup_file,
    load_growth_for_prompt,
    load_text,
    normalize,
    read_consolidation_data,
    read_growth_entries,
    read_agent_file,
    safe_write_memory,
    split_by_date,
    split_events_raw,
    write_consolidation_data,
    write_growth_entries,
)
from memory.vector_store import vector_store

_PROMPT_STEP1_PATH = Path(__file__).parent.parent / "prompts" / "memory_scene_merge.txt"
_PROMPT_STEP2_PATH = Path(__file__).parent.parent / "prompts" / "growth_extract.txt"
_PROMPT_STEP3_PATH = Path(__file__).parent.parent / "prompts" / "growth_dedupe.txt"
_PLAYER_PROMPT_PATH = (
    Path(__file__).parent.parent / "prompts" / "player_profile_consolidation_prompt.txt"
)


def _load_raw_dialogue_for_agent(agent_name: str, limit: int) -> str:
    """读取最近 limit 条原始消息，按 visible_to 过滤后格式化为纯文本供 step1 参考。"""
    raw_messages = load_conversation_history(limit=limit)

    if agent_name != "narrator":
        visible = [m for m in raw_messages if agent_name in m.get("visible_to", [])]
    else:
        visible = raw_messages

    if not visible:
        return ""

    lines = []
    for msg in visible:
        role = msg.get("role", "unknown")
        content = msg.get("content", "").strip()
        if content:
            lines.append(f"{role}: {content}")

    return "\n\n".join(lines)


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


def _resolve_dates(
    agent_name: str,
    all_dates: list[str],
    last_consolidated: str | None,
) -> tuple[list[str], str | None] | None:
    """纯函数：根据进度指针确定本次需要整合的日期范围。

    Returns:
        (dates_to_consolidate, next_date) 或 None（无法解析进度）
    """
    if last_consolidated:
        if last_consolidated not in all_dates:
            routing_logger.warning(
                f"[整理器] {agent_name} 进度日期 '{last_consolidated}' 在文件中不存在"
            )
            return all_dates, all_dates[-1] if all_dates else None

        current_idx = all_dates.index(last_consolidated)
        if current_idx == len(all_dates) - 1:
            # 没有新日期，继续整理当前日期
            routing_logger.info(
                f"[整理器] {agent_name} 整理日期: {last_consolidated}"
            )
            return [last_consolidated], last_consolidated

        # 有新日期出现：最后一次整理当前日期，同时整理所有新日期
        new_dates = all_dates[current_idx + 1:]
        routing_logger.info(
            f"[整理器] {agent_name} 检测到新日期，最后一次整理 {last_consolidated}，"
            f"同时整理新日期: {', '.join(new_dates)}"
        )
        return all_dates[current_idx:], all_dates[-1]

    # 无进度记录，整理全部
    routing_logger.info(
        f"[整理器] {agent_name} 无进度记录，从头开始整理全部 {len(all_dates)} 个日期"
    )
    return all_dates, all_dates[-1] if all_dates else None


def _should_skip(
    raw_dialogue: str,
    current_size: int,
    last_size: int | None,
) -> str | None:
    """如果应跳过整理，返回跳过原因；否则返回 None。无 IO、无副作用。"""
    if not raw_dialogue:
        return f"最近 {RAW_DIALOGUE_LIMIT} 条消息中无参与"
    if last_size is not None and (current_size - last_size) < CONSOLIDATION_SIZE_THRESHOLD:
        return f"文件增长不足 {CONSOLIDATION_SIZE_THRESHOLD} 字 ({last_size}→{current_size})"
    return None


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

    def _build_consolidation_prompt_step1(
        self, agent_name: str, sections: OrderedDict[str, str], dates: list[str],
        raw_dialogue: str = "",
    ) -> tuple[str, str]:
        """构建第一步 prompt：归并整理（角色带 growth，narrator 不带）。

        Returns:
            (system, user): system 为纯指令，user 为结构化数据 + 待整理内容
        """
        parts = [f"## {date}\n{sections[date]}" for date in dates]
        combined_text = "\n\n".join(parts)

        system = load_text(_PROMPT_STEP1_PATH)
        user = f"<memory_entries>\n{combined_text}\n</memory_entries>"

        if raw_dialogue:
            user += f"\n\n<raw_dialogue>\n{raw_dialogue}\n</raw_dialogue>"

        return system, user

    def _build_consolidation_prompt_step2(
        self, agent_name: str, step1_result: str
    ) -> tuple[str, str]:
        """构建第二步 prompt：成长事件判断（soul/growth/input 放 user message）。

        Returns:
            (system, user): system 为纯指令，user 为结构化数据 + 第一步整理结果
        """
        soul_content = read_agent_file(agent_name, "soul.md")
        growth_content = load_growth_for_prompt(agent_name, default="（尚无）")

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
        growth_content = load_growth_for_prompt(agent_name, default="（尚无）")
        system = load_text(_PROMPT_STEP3_PATH)
        user = f"<existing_growth>\n{growth_content}\n</existing_growth>"
        return system, user

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
        sections: OrderedDict[str, str],
        dates: list[str],
        raw_dialogue: str = "",
    ) -> list[str]:
        """执行三步 LLM 整理 pipeline，就地更新 sections，返回错误列表。"""
        errors: list[str] = []

        async with OpenAICompatibleClient(
            **get_consolidation_llm_config(temperature=CONSOLIDATION_TEMPERATURE),
            max_tokens=CONSOLIDATION_MAX_TOKENS, timeout=120.0, max_retries=3,
        ) as client:
            # ===== 第一步：调用 LLM 进行归并整理 =====
            system_step1, user_step1 = self._build_consolidation_prompt_step1(
                agent_name, sections, dates, raw_dialogue=raw_dialogue
            )
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
                return errors

            if len(step1_result) < 50:
                errors.append("第一步返回过短，跳过整理")
                return errors

            # 解析第一步结果，仅对通过字段校验的日期就地更新 sections
            step1_sections = self._parse_step1_memories(step1_result)
            if step1_sections:
                for date in dates:
                    if date in step1_sections:
                        missing = self._check_event_fields(step1_sections[date])
                        if missing:
                            routing_logger.warning(
                                f"[整理器] {agent_name} {date} 整理结果缺少字段 {missing}，保留原始内容"
                            )
                            errors.append(f"{date} 字段不完整({','.join(missing)})，已跳过")
                        else:
                            sections[date] = step1_sections[date]
                    else:
                        errors.append(f"{date} 未在第一步返回中找到")
            else:
                errors.append("未能解析第一步:归并整理")
                return errors

            if not self._supports_growth(agent_name):
                routing_logger.info(f"[整理器] {agent_name} 跳过 growth.md 流程")
                return errors

            # ===== 第二步：调用 LLM 进行成长事件判断 =====
            system_step2, user_step2 = self._build_consolidation_prompt_step2(agent_name, step1_result)
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
                return errors

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

        return errors

    async def consolidate_agent(
        self, agent_name: str
    ) -> "_ConsolidationResult | None":
        """整理单个 agent 的记忆，返回结果摘要（供 consolidate_all 汇总日志）"""
        result = _ConsolidationResult(agent_name=agent_name)
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

            # 2. 确定需要整合的日期
            all_dates = list(sections.keys())
            cdata = read_consolidation_data(agent_name)
            last_consolidated = cdata.get("last_consolidated_date")
            resolved = _resolve_dates(agent_name, all_dates, last_consolidated)
            if resolved is None:
                return None
            dates, next_date = resolved
            if not dates:
                return None

            # 3. guard 检查：近期无参与 / 文件增长不足
            raw_dialogue = _load_raw_dialogue_for_agent(agent_name, RAW_DIALOGUE_LIMIT)
            if reason := _should_skip(raw_dialogue, len(original_content), cdata.get("last_memory_size")):
                result.skipped, result.skip_reason = True, reason
                routing_logger.info(f"[整理器] {agent_name} 跳过: {reason}")
                return result

            # 4. 备份 → LLM pipeline → 写回
            result.days, result.date_range = len(dates), f"{dates[0]}~{dates[-1]}"
            result.original_len = len(original_content)
            backup_file(path, agent_name, "Memory")

            try:
                result.errors.extend(
                    await self._run_memory_pipeline(agent_name, sections, dates, raw_dialogue)
                )
            except Exception as e:
                result.errors.append(f"整合失败: {e}")
                routing_logger.error(f"[整理器] {agent_name} 整合失败: {e}")

            result.final_len = safe_write_memory(path, sections, agent_name, original_content)
            if result.final_len < 0:
                result.errors.append("并发冲突：检测到中间变更，已放弃写回")
                return result

            # 5. 向量同步 + 进度更新
            if last_consolidated:
                await vector_store.add_memory(agent_name, last_consolidated)
            if next_date and not result.errors:
                if last_consolidated and next_date != last_consolidated:
                    routing_logger.info(f"[整理器] {agent_name} 进度推进: {last_consolidated} → {next_date}")
                write_consolidation_data(agent_name, last_consolidated_date=next_date)
            if not result.errors:
                write_consolidation_data(agent_name, last_memory_size=result.final_len)

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

    def _parse_step1_memories(self, llm_result: str) -> OrderedDict[str, str]:
        """
        从第一步 LLM 输出中提取归并整理后的日记内容。

        格式（扁平列表，日期在时间字段中）：
        - **时间**：4月3日 上午
        - **地点**：教室
        - **在场**：莉莉丝、李小明
        - **内容**：事件描述...

        Returns:
            OrderedDict[日期, 该日期的内容]
        """
        # 移除 analysis 标签及其内容（工作流第一步的分析部分）
        cleaned = re.sub(r"<analysis>.*?</analysis>", "", llm_result, flags=re.DOTALL)
        cleaned = cleaned.strip()

        sections: OrderedDict[str, str] = OrderedDict()
        for date, event_text in split_events_raw(cleaned):
            if not date:
                continue
            sections[date] = (sections.get(date, "") + ("\n\n" if date in sections else "") + event_text)
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
