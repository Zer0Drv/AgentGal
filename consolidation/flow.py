"""后台记忆整理流程。"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from agents.factory import (
    get_episode_closure_detector_agent,
    get_episode_memory_generator_agent,
    get_growth_dedup_agent,
    get_growth_extract_agent,
    get_player_profile_agent,
)
from agents.runner import run_structured_agent, run_text_agent
from log_config.memory import memory_logger
from llm.providers import get_consolidation_llm_config
from shared.config import (
    AGENT_RUN_TIMEOUT_SECONDS,
    CONSOLIDATION_TEMPERATURE,
    GROWTH_DEDUP_THRESHOLD,
    character_path,
    get_agent_names,
)
from shared.text_utils import get_display_name
from consolidation.inputs import (
    build_episode_closure_payload,
    build_episode_memory_generator_payload,
    render_raw_history,
)
from storage.agent_files import (
    get_fields_from_file,
    backup_file,
    read_agent_file,
    read_growth_entries,
    read_memory_draft,
    rewrite_memory_draft,
    split_memory_draft_by_turn,
    write_growth_entries,
)
from storage.history import load_conversation_history
from memory.parser import (
    EpisodeMemory,
    append_memory_records,
    canonical_cn_date,
    memory_jsonl_path,
)
from storage.vector_store import vector_store
from agents.schema import (
    EpisodeClosureOutput,
    EpisodeMemoryBlock,
    GrowthDedupOutput,
    GrowthExtractOutput,
)


# ---------------------------------------------------------------------------
# 辅助函数（非 LLM）
# ---------------------------------------------------------------------------


def _episode_to_llm_payload(episode: EpisodeMemory) -> dict[str, str]:
    """把 EpisodeMemory 压成喂给 growth LLM 的 dict。

    剔除 keywords/importance：growth 阶段用不到。
    memory_owner 是调用方上下文，不喂给 LLM。
    """
    return {
        "date": episode.date,
        "time": episode.time,
        "location": episode.location,
        "participants": episode.participants,
        "title": episode.title,
        "content": episode.content,
    }


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


def _normalize_episode_memory_time(time_text: str, date_text: str | None) -> str:
    if not time_text:
        return time_text

    explicit_date = canonical_cn_date(time_text)
    if explicit_date:
        if not date_text:
            return time_text
        remainder = re.sub(r"\d{1,2}月\d{1,2}日", "", time_text, count=1).strip()
        return f"{date_text} {remainder}".strip()

    if date_text:
        return f"{date_text} {time_text}".strip()
    return time_text



# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


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
            timeout_seconds=AGENT_RUN_TIMEOUT_SECONDS,
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
    ) -> EpisodeMemory:
        """EpisodeMemoryGenerator 对单个闭合 episode 生成一条记忆。

        raw_dialogue 既作为事实校正材料送入 LLM，也原样注入到落盘的 EpisodeMemory，
        保留源对话的可追溯性（不进向量索引、不进召回文本）。
        """
        user = build_episode_memory_generator_payload(agent_name, memory_entries, raw_dialogue)
        block = await self._run_consolidation_agent(
            agent=get_episode_memory_generator_agent(),
            output_type=EpisodeMemoryBlock,
            agent_name=agent_name,
            function_name="episode_memory_generator",
            user=user,
        )

        if not all([block.date, block.time, block.location, block.participants, block.content]):
            raise ValueError(f"{block.date} 输出块结构不完整（存在空字段）")
        normalized_date = canonical_cn_date(block.date) or canonical_cn_date(block.time)
        return EpisodeMemory(
            date=normalized_date or block.date,
            time=_normalize_episode_memory_time(block.time, normalized_date),
            location=block.location,
            participants=block.participants,
            keywords=block.keywords,
            importance=block.importance,
            content=block.content,
            memory_owner=agent_name,
            title=block.title,
            raw_dialogue=raw_dialogue,
        )

    async def _extract_growth_updates(
        self,
        agent_name: str,
        episode: EpisodeMemory,
    ) -> None:
        payload = json.dumps(
            [_episode_to_llm_payload(episode)],
            ensure_ascii=False,
            indent=2,
        )
        soul_content = read_agent_file(agent_name, "soul.md")
        growth_content = read_agent_file(agent_name, "growth.md") or "（尚无）"
        user = (
            f"<soul>\n{soul_content}\n</soul>\n\n"
            f"<existing_growth>\n{growth_content}\n</existing_growth>\n\n"
            f"<consolidated_memory>\n{payload}\n</consolidated_memory>"
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
            memory_logger.info(
                f"[整理器] {agent_name} growth.md: {self._apply_growth_updates(agent_name, updates)}"
            )
        else:
            memory_logger.info(f"[整理器] {agent_name} 无人格沉淀更新")

    async def _dedup_growth_entries(self, agent_name: str) -> None:
        current_count = len(read_growth_entries(agent_name))
        if current_count <= GROWTH_DEDUP_THRESHOLD:
            memory_logger.info(
                f"[整理器] {agent_name} 跳过 growth dedup（当前 {current_count} 条，未超过阈值 {GROWTH_DEDUP_THRESHOLD}）"
            )
            return
        memory_logger.info(
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
            memory_logger.warning(f"[整理器] {agent_name} growth dedup 结果为空，跳过")
            return

        growth_path = Path(character_path(agent_name, "growth.md"))
        if growth_path.exists():
            backup_file(growth_path, agent_name, "growth")

        write_growth_entries(
            agent_name,
            {f"P{i:03d}": entry for i, entry in enumerate(entries, start=1)},
        )
        memory_logger.info(f"[整理器] {agent_name} growth dedup 完成，条目数: {len(entries)}")

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
        raw_dialogue: str = "",
    ) -> tuple[EpisodeMemory | None, list[str]]:
        errors: list[str] = []

        try:
            episode = await self._merge_memory_blocks(
                agent_name, memory_entries, raw_dialogue
            )
        except Exception as e:
            errors.append(f"第一步调用失败: {e}")
            memory_logger.error(f"[整理器] {agent_name} 第一步调用失败: {e}")
            return None, errors

        if not self._supports_growth(agent_name):
            memory_logger.info(f"[整理器] {agent_name} 跳过 growth.md 流程")
            return episode, errors

        try:
            await self._extract_growth_updates(agent_name, episode)
        except Exception as e:
            errors.append(f"第二步调用失败: {e}")
            memory_logger.error(f"[整理器] {agent_name} 第二步调用失败: {e}")
            return episode, errors

        try:
            await self._dedup_growth_entries(agent_name)
        except Exception as e:
            errors.append(f"第三步调用失败: {e}")
            memory_logger.error(f"[整理器] {agent_name} 第三步调用失败: {e}")

        return episode, errors

    def _prepare_consolidation_slice(
        self,
        agent_name: str,
        until_turn: int,
        raw_messages: list[dict],
    ) -> tuple[list[dict], list[dict], str, str] | None:
        """按 until_turn 切 memory_draft.jsonl，并构造对应的 memory_entries / raw_dialogue。

        返回 (taken_records, remaining_records, memory_entries, raw_dialogue)；
        若 draft 切片为空或未覆盖到原始对话，返回 None。
        """
        taken, remaining = split_memory_draft_by_turn(agent_name, until_turn)
        if not taken:
            return None

        memory_entries = "\n\n".join(
            f"[turn={int(r.get('turn', 0))}] {(r.get('text') or '').strip()}"
            for r in taken
            if (r.get("text") or "").strip()
        )
        if not memory_entries:
            return None

        turn_ge = min(int(r.get("turn", 0)) for r in taken)
        raw_dialogue = render_raw_history(
            raw_messages,
            visible_to=agent_name,
            turn_ge=turn_ge,
            turn_le=until_turn,
        )
        return taken, remaining, memory_entries, raw_dialogue

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
            timeout_seconds=AGENT_RUN_TIMEOUT_SECONDS,
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
                memory_logger.warning(f"[整理器] {agent_name} user.md LLM 返回过短，跳过")
                return 0, 0

            user_path.write_text(consolidated.strip() + "\n", encoding="utf-8")
            tmp_path.unlink(missing_ok=True)
            before_len, after_len = len(user_content), len(consolidated)
            memory_logger.info(
                f"[整理器] {agent_name} user.md 整理完成 (长度: {before_len} → {after_len})"
            )
            return before_len, after_len
        except Exception as e:
            memory_logger.error(f"[整理器] {agent_name} user.md 整理失败: {e}")
            return 0, 0

    async def consolidate_agent(
        self,
        agent_name: str,
        until_turn: int,
        raw_messages: list[dict] | None = None,
    ) -> _ConsolidationResult | None:
        """合并该角色 memory_draft 中 turn <= until_turn 的记忆草稿为一条 EpisodeMemory。

        raw_messages 由 detect_and_consolidate 统一加载后传入，避免每个闭合角色重复扫描
        整个 raw JSONL；为空时退化为自己加载（保留直接调用 / 测试入口的便利）。
        """
        result = _ConsolidationResult(agent_name=agent_name)
        if agent_name == "narrator":
            result.skipped, result.skip_reason = True, "旁白不维护 memory.jsonl，也不参与整理"
            return result

        lock = self._get_lock(agent_name)
        if lock.locked():
            result.skipped, result.skip_reason = True, "已有整理任务在运行"
            return result

        async with lock:
            if raw_messages is None:
                raw_messages = load_conversation_history(limit=None)
            slice_data = self._prepare_consolidation_slice(
                agent_name, until_turn, raw_messages
            )
            if slice_data is None:
                result.skipped, result.skip_reason = True, (
                    f"memory_draft.jsonl 在 turn<={until_turn} 区间无可整理内容"
                )
                memory_logger.info(f"[整理器] {agent_name} 跳过: {result.skip_reason}")
                return result
            taken, remaining, memory_entries, raw_dialogue = slice_data

            jsonl_path = memory_jsonl_path(agent_name)
            result.original_len = jsonl_path.stat().st_size if jsonl_path.exists() else 0
            if jsonl_path.exists() and result.original_len > 0:
                backup_file(jsonl_path, agent_name, "Memory")

            episode: EpisodeMemory | None = None
            try:
                episode, pipeline_errors = await self._apply_consolidation_pipeline(
                    agent_name,
                    memory_entries,
                    raw_dialogue,
                )
                result.errors.extend(pipeline_errors)
            except Exception as e:
                result.errors.append(f"整合失败: {e}")
                memory_logger.error(f"[整理器] {agent_name} 整合失败: {e}")

            if episode is None:
                return result

            appended = append_memory_records(agent_name, [episode])
            unique_dates = list(dict.fromkeys(ep.date for ep in appended))
            result.days = len(unique_dates)
            if unique_dates:
                result.date_range = f"{unique_dates[0]}~{unique_dates[-1]}"

            result.final_len = jsonl_path.stat().st_size if jsonl_path.exists() else 0

            rewrite_memory_draft(agent_name, remaining)

            for ep in appended:
                await vector_store.add(ep)

            user_before, user_after = await self._consolidate_player_profile(agent_name)
            result.user_md_before, result.user_md_after = user_before, user_after

            memory_logger.info(
                f"[整理器] {agent_name} 完成: 归并 {len(taken)} 条 draft → 1 条 EpisodeMemory "
                f"(until_turn={until_turn})"
            )
            return result

    async def run_player_profile_consolidation(
        self,
        agent_name: str,
        draft_content: str,
    ) -> str | None:
        try:
            return await self._call_player_profile_agent(agent_name, draft_content)
        except Exception as e:
            memory_logger.error(f"[整理器] {agent_name} user.md 整理失败: {e}")
            return None

    def _collect_candidates(self) -> tuple[list[tuple[str, str]], int | None]:
        """扫描角色目录，返回 (candidates, earliest_draft_turn)。

        earliest_draft_turn 是所有候选 draft 里最早的 turn；早于此的 raw 历史已归并，
        对本轮闭合判断无关，作为 closure detector 的窗口下界。
        """
        candidates: list[tuple[str, str]] = []
        earliest: int | None = None
        for name in get_agent_names(include_narrator=False):
            draft = read_memory_draft(name)
            if not draft:
                continue
            soul = read_agent_file(name, "soul.md") or ""
            display = get_display_name(name, soul) if soul else name
            candidates.append((name, display))
            for record in draft:
                turn = record.get("turn")
                if isinstance(turn, int) and turn > 0:
                    if earliest is None or turn < earliest:
                        earliest = turn
        return candidates, earliest

    async def _detect_closures(
        self,
        candidates: list[tuple[str, str]],
        raw_messages: list[dict],
        earliest_draft_turn: int | None,
        current_turn: int,
    ) -> dict[str, int]:
        """调 EpisodeClosureDetector，返回 {agent_name: closed_at_turn}。"""
        history_transcript = render_raw_history(raw_messages, turn_ge=earliest_draft_turn)
        if not history_transcript:
            return {}

        user = build_episode_closure_payload(candidates, history_transcript, current_turn)
        try:
            output = await self._run_consolidation_agent(
                agent=get_episode_closure_detector_agent(),
                output_type=EpisodeClosureOutput,
                agent_name="_closure_detector",
                function_name="episode_closure_detector",
                user=user,
            )
        except Exception as e:
            memory_logger.error(f"[整理器] closure detector 调用失败: {e}")
            return {}

        candidate_names = {name for name, _ in candidates}
        cleaned: dict[str, int] = {}
        for name, turn in output.root.items():
            if name not in candidate_names:
                continue
            try:
                cleaned[name] = int(turn)
            except (TypeError, ValueError):
                continue
        return cleaned

    async def detect_and_consolidate(self, current_turn: int) -> None:
        """回合末入口：扫描候选 → 判定闭合 → 并行归并闭合角色。

        无候选或无闭合时静默返回；失败不影响主流程。
        """
        candidates, earliest_draft_turn = self._collect_candidates()
        if not candidates:
            return

        raw_messages = load_conversation_history(limit=None)
        closures = await self._detect_closures(
            candidates, raw_messages, earliest_draft_turn, current_turn
        )
        if not closures:
            memory_logger.info(
                f"[整理器] turn={current_turn} 无角色闭合 "
                f"(候选: {[n for n, _ in candidates]})"
            )
            return

        memory_logger.info(f"[整理器] turn={current_turn} 闭合: {closures}")
        t0 = time.monotonic()
        await asyncio.gather(
            *(
                self.consolidate_agent(name, until_turn=turn, raw_messages=raw_messages)
                for name, turn in closures.items()
            ),
            return_exceptions=True,
        )
        memory_logger.info(f"[整理器] 闭合归并完成 (耗时 {time.monotonic() - t0:.1f}s)")


memory_consolidation_flow = MemoryConsolidationFlow()
