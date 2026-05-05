"""后台记忆整理流程。"""

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field

from agents.factory import (
    get_episode_closure_detector_agent,
    get_episode_memory_generator_agent,
    get_understanding_patch_agent,
)
from agents.runner import run_structured_agent
from log_config.memory import memory_logger
from llm.providers import get_consolidation_llm_config
from shared.config import (
    AGENT_RUN_TIMEOUT_SECONDS,
    CONSOLIDATION_TEMPERATURE,
    get_agent_names,
)
from consolidation.inputs import (
    build_episode_closure_payload,
    build_episode_memory_generator_payload,
    build_understanding_patch_payload,
    render_raw_history,
)
from storage.agent_files import (
    backup_file,
    read_memory_draft,
    rewrite_memory_draft,
    split_memory_draft_by_turn,
)
from storage.history import load_conversation_history
from memory.parser import (
    EpisodeMemory,
    Understanding,
    UnderstandingHistoryEntry,
    append_memory_records,
    canonical_cn_date,
    memory_jsonl_path,
    read_understandings,
    write_understandings,
)
from storage.vector_store import vector_store
from agents.schema import (
    EpisodeClosureOutput,
    EpisodeMemoryBlock,
    UnderstandingPatchOutput,
)


# ---------------------------------------------------------------------------
# 辅助函数（非 LLM）
# ---------------------------------------------------------------------------


@dataclass
class _UnderstandingPatchResult:
    updated: dict[str, Understanding] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    links_only_ids: list[str] = field(default_factory=list)
    full_replace_ids: list[str] = field(default_factory=list)
    added_ids: list[str] = field(default_factory=list)


def _episode_to_llm_payload(episode: EpisodeMemory) -> dict[str, str]:
    return {
        "id": episode.id,
        "date": episode.date,
        "time": episode.time,
        "location": episode.location,
        "participants": episode.participants,
        "title": episode.title,
        "content": episode.content,
    }


def _render_understandings_for_prompt(understandings: dict[str, Understanding]) -> str:
    if not understandings:
        return "（尚无）"
    lines: list[str] = []
    for uid, u in understandings.items():
        keywords = "、".join(u.keywords) if u.keywords else ""
        keywords_part = f"\n  keywords: {keywords}" if keywords else ""
        lines.append(
            f"[{uid}] subject={u.subject!r}{keywords_part}\n"
            f"  content: {u.content}"
        )
    return "\n\n".join(lines)


def _apply_understanding_patch(
    agent_name: str,
    understandings: dict[str, Understanding],
    patch: UnderstandingPatchOutput,
    episode: EpisodeMemory | None = None,
) -> _UnderstandingPatchResult:
    updated = dict(understandings)
    logs: list[str] = []
    links_only_ids: list[str] = []
    full_replace_ids: list[str] = []
    added_ids: list[str] = []

    new_episode_id = episode.id if episode and episode.id else ""

    for uid, entry in patch.update.items():
        if uid not in updated:
            memory_logger.warning(
                f"[整理器] {agent_name} understanding patch update 跳过不存在 ID: {uid}"
            )
            continue
        if not entry.content:
            memory_logger.warning(
                f"[整理器] {agent_name} understanding patch update 跳过空 content: {uid}"
            )
            continue
        existing = updated[uid]
        new_subject = entry.subject if entry.subject else existing.subject
        new_keywords = entry.keywords if entry.keywords else existing.keywords
        injected = [new_episode_id] if new_episode_id else []
        linked_episodes = list(
            dict.fromkeys([*existing.linked_episodes, *injected])
        )
        content_changed = entry.content != existing.content
        history = list(existing.history)
        if content_changed:
            history.append(
                UnderstandingHistoryEntry(
                    episode_id=episode.id if episode else "",
                    date=episode.date if episode else "",
                    title=episode.title if episode else "",
                    content=entry.content,
                )
            )
        updated[uid] = Understanding(
            id=uid,
            memory_owner=agent_name,
            subject=new_subject,
            keywords=new_keywords,
            content=entry.content,
            linked_episodes=linked_episodes,
            history=history,
        )
        if (
            not content_changed
            and new_subject == existing.subject
            and new_keywords == existing.keywords
        ):
            links_only_ids.append(uid)
        else:
            full_replace_ids.append(uid)
        logs.append(f"UPDATE {uid}")

    for entry in patch.add:
        if not entry.content:
            memory_logger.warning(
                f"[整理器] {agent_name} understanding patch add 跳过空 content"
            )
            continue
        new_id = uuid.uuid4().hex
        history = [
            UnderstandingHistoryEntry(
                episode_id=episode.id if episode else "",
                date=episode.date if episode else "",
                title=episode.title if episode else "",
                content=entry.content,
            )
        ]
        updated[new_id] = Understanding(
            id=new_id,
            memory_owner=agent_name,
            subject=entry.subject,
            keywords=entry.keywords,
            content=entry.content,
            linked_episodes=[new_episode_id] if new_episode_id else [],
            history=history,
        )
        added_ids.append(new_id)
        logs.append(f"ADD {new_id}")

    return _UnderstandingPatchResult(
        updated=updated,
        logs=logs,
        links_only_ids=links_only_ids,
        full_replace_ids=full_replace_ids,
        added_ids=added_ids,
    )


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
    original_len: int = 0
    final_len: int = 0
    skipped: bool = False
    skip_reason: str = ""
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# MemoryConsolidationFlow
# ---------------------------------------------------------------------------


class MemoryConsolidationFlow:
    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        # 跟踪正在执行的 detect_and_consolidate 任务数；存档/读档需在为 0 时才允许进行
        self._active_count: int = 0

    @property
    def is_running(self) -> bool:
        """是否有 detect_and_consolidate 任务正在执行（含其内部的 closure detector / consolidate_agent）。"""
        return self._active_count > 0

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

    async def _patch_understandings(
        self,
        agent_name: str,
        episode: EpisodeMemory,
    ) -> None:
        current_understandings = read_understandings(agent_name)
        existing_text = _render_understandings_for_prompt(current_understandings)
        episode_json = json.dumps(
            _episode_to_llm_payload(episode), ensure_ascii=False, indent=2
        )
        user = build_understanding_patch_payload(existing_text, episode_json)

        output = await self._run_consolidation_agent(
            agent=get_understanding_patch_agent(),
            output_type=UnderstandingPatchOutput,
            agent_name=agent_name,
            function_name="understanding_patch",
            user=user,
        )

        result = _apply_understanding_patch(
            agent_name, current_understandings, output, episode
        )
        if not result.logs:
            memory_logger.debug(f"[整理器] {agent_name} 无 Understanding 更新")
            return

        write_understandings(agent_name, result.updated)
        memory_logger.info(
            f"[整理器] {agent_name} understanding patch 完成: {';'.join(result.logs)}"
        )

        for uid in result.links_only_ids:
            await vector_store.update_understanding_links(uid, result.updated[uid].linked_episodes)
        for uid in result.full_replace_ids:
            await vector_store.delete_understanding(uid)
            await vector_store.add_understanding(result.updated[uid])
        for uid in result.added_ids:
            await vector_store.add_understanding(result.updated[uid])

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

        if not episode.id:
            episode = episode.model_copy(update={"id": uuid.uuid4().hex})

        try:
            await self._patch_understandings(agent_name, episode)
        except Exception as e:
            errors.append(f"第二步（understanding patch）调用失败: {e}")
            memory_logger.error(f"[整理器] {agent_name} 第二步调用失败: {e}")

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
                raw_messages = load_conversation_history()
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
            if jsonl_path.exists():
                result.original_len = jsonl_path.stat().st_size
                if result.original_len > 0:
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
            result.final_len = jsonl_path.stat().st_size if jsonl_path.exists() else 0

            rewrite_memory_draft(agent_name, remaining)

            for ep in appended:
                await vector_store.add(ep)

            memory_logger.info(
                f"[整理器] {agent_name} 完成: 归并 {len(taken)} 条 draft → 1 条 EpisodeMemory "
                f"(until_turn={until_turn})"
            )
            return result

    def _collect_candidates(self) -> tuple[list[str], int | None]:
        """扫描角色目录，返回 (candidates, earliest_draft_turn)。

        earliest_draft_turn 是所有候选 draft 里最早的 turn；早于此的 raw 历史已归并，
        对本轮闭合判断无关，作为 closure detector 的窗口下界。
        """
        candidates: list[str] = []
        earliest: int | None = None
        for name in get_agent_names(include_narrator=False):
            draft = read_memory_draft(name)
            if not draft:
                continue
            candidates.append(name)
            for record in draft:
                turn = record.get("turn")
                if isinstance(turn, int) and turn > 0:
                    if earliest is None or turn < earliest:
                        earliest = turn
        return candidates, earliest

    async def _detect_closures(
        self,
        candidates: list[str],
        raw_messages: list[dict],
        earliest_draft_turn: int | None,
        latest_open_turn: int | None,
    ) -> dict[str, int]:
        """调 EpisodeClosureDetector，返回 {agent_name: closed_at_turn}。"""
        history_transcript = render_raw_history(raw_messages, turn_ge=earliest_draft_turn)
        if not history_transcript:
            return {}

        user = build_episode_closure_payload(history_transcript)
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

        candidate_names = set(candidates)
        cleaned: dict[str, int] = {}
        for name, boundaries in output.root.items():
            if name not in candidate_names or not boundaries:
                continue
            closed_turns = [
                b.end_turn
                for b in boundaries
                if latest_open_turn is None or b.end_turn < latest_open_turn
            ]
            if not closed_turns:
                continue
            cleaned[name] = max(closed_turns)
        return cleaned

    async def _consolidation_pipeline(self, current_turn: int) -> None:
        """实际整理流程主体：扫描候选 → 判定闭合 → 并行归并闭合角色。"""
        candidates, earliest_draft_turn = self._collect_candidates()
        if not candidates:
            return

        raw_messages = load_conversation_history()
        closures = await self._detect_closures(
            candidates,
            raw_messages,
            earliest_draft_turn,
            latest_open_turn=current_turn,
        )
        if not closures:
            memory_logger.info(
                f"[整理器] turn={current_turn} 无角色闭合 "
                f"(候选: {candidates})"
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

    async def detect_and_consolidate(self, current_turn: int) -> None:
        """回合末入口（直接 await 版本）。无候选或无闭合时静默返回；失败不影响主流程。"""
        self._active_count += 1
        try:
            await self._consolidation_pipeline(current_turn)
        finally:
            self._active_count -= 1

    def schedule_detect_and_consolidate(self, current_turn: int) -> asyncio.Task:
        """后台调度入口：返回前同步把 is_running 置为 True，避免 create_task 与
        SSE done 事件之间的竞态——调用方立刻读 is_running 就能拿到准确状态。
        """
        self._active_count += 1

        async def _runner() -> None:
            try:
                await self._consolidation_pipeline(current_turn)
            finally:
                self._active_count -= 1

        return asyncio.create_task(_runner())


memory_consolidation_flow = MemoryConsolidationFlow()
