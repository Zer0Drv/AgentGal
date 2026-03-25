"""后台记忆整理器。"""

import asyncio
import hashlib
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
from engine.agent_files import (
    get_fields_from_file,
    backup_file,
    load_text,
    read_growth_entries,
    read_agent_file,
    write_growth_entries,
)
from memory.parser import (
    extract_event_field,
    is_structured_memory_block,
    normalize,
    parse_event_importance,
    parse_llm_memory_sections,
    render_sections,
    split_by_date,
    split_into_events,
)
from memory.vector_store import vector_store

_PROMPT_STEP1_PATH = Path(__file__).parent.parent / "prompts" / "memory_scene_merge.txt"
_PROMPT_STEP1_5_PATH = Path(__file__).parent.parent / "prompts" / "memory_chunk_metadata.txt"
_PROMPT_STEP2_PATH = Path(__file__).parent.parent / "prompts" / "growth_extract.txt"
_PROMPT_STEP3_PATH = Path(__file__).parent.parent / "prompts" / "growth_dedupe.txt"
_PLAYER_PROMPT_PATH = (
    Path(__file__).parent.parent / "prompts" / "player_profile_consolidation_prompt.txt"
)
_STEP1_REQUIRED_FIELDS = ("时间", "地点", "在场", "内容")


def safe_write_memory(
    path: Path,
    sections: dict[str, str],
    agent_name: str,
    original_content: str,
) -> tuple[int, int]:
    """安全写回 memory.md，带最小并发保护。

    策略：
    - 若 current 以 original 开头，视为尾部追加，保留该追加；
    - 若 current == original，正常覆盖；
    - 否则判定为中间变更，放弃写回并返回 (-1, -1)。

    Returns:
        (写入后的文件长度, 已整理快照长度)，(-1, -1) 表示检测到并发冲突放弃写回
    """
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


def build_fields_definition(agent_name: str) -> str:
    file_path = character_path(agent_name, "user.md")
    fields = get_fields_from_file(file_path) or ["基本信息", "对方是什么人", "我们怎么相处"]
    return "\n".join(f"- 「{field}」：{_USER_FIELD_DESCRIPTIONS.get(field, '')}" for field in fields)


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


def _block_fingerprint(date: str, content: str) -> str:
    normalized_lines = [line.rstrip() for line in content.strip().splitlines()]
    normalized = "\n".join(normalized_lines).strip()
    return hashlib.sha1(f"{date}\n{normalized}".encode("utf-8")).hexdigest()


def _make_block(date: str, content: str) -> dict[str, str]:
    stripped = content.strip()
    return {
        "date": date,
        "content": stripped,
        "fingerprint": _block_fingerprint(date, stripped),
    }


def _is_structured_block(block: dict[str, str]) -> bool:
    return is_structured_memory_block(block["content"])


def _flatten_sections(sections: OrderedDict[str, str]) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    for date, body in sections.items():
        for content in split_into_events(body):
            stripped = content.strip()
            if stripped:
                blocks.append(_make_block(date, stripped))
    return blocks


def _group_blocks(blocks: list[dict[str, str]]) -> OrderedDict[str, str]:
    sections: OrderedDict[str, str] = OrderedDict()
    for block in blocks:
        date = block["date"]
        sections[date] = sections.get(date, "") + ("\n\n" if date in sections else "") + block["content"].strip()
    return sections


def _normalize_keywords(keywords: str) -> str:
    return " ".join((keywords or "").split())


def _apply_chunk_metadata(event_text: str, keywords: str, importance: int) -> str:
    normalized_keywords = _normalize_keywords(keywords)
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
        _make_block(
            block["date"],
            _apply_chunk_metadata(
                block["content"],
                extract_event_field(block["content"], "关键词"),
                parse_event_importance(block["content"], default=3),
            ),
        )
        for block in blocks
    ]


def _validate_step1_result(
    sections: OrderedDict[str, str],
    expected_dates: list[str] | None = None,
) -> str | None:
    invalid_block = next(
        (
            block
            for block in _flatten_sections(sections)
            if not is_structured_memory_block(block["content"], required_fields=_STEP1_REQUIRED_FIELDS)
        ),
        None,
    )
    if invalid_block:
        return f"{invalid_block['date']} 输出块结构不完整，已跳过"

    if expected_dates:
        missing_dates = [date for date in expected_dates if date not in sections]
        if missing_dates:
            preview = "、".join(missing_dates[:5])
            suffix = f" 等{len(missing_dates)}天" if len(missing_dates) > 5 else ""
            return f"输出缺少日期：{preview}{suffix}"

        unexpected_dates = [date for date in sections if date not in expected_dates]
        if unexpected_dates:
            preview = "、".join(unexpected_dates[:5])
            suffix = f" 等{len(unexpected_dates)}天" if len(unexpected_dates) > 5 else ""
            return f"输出包含窗口外日期：{preview}{suffix}"

    return None



def _parse_step1_5_metadata(llm_result: str) -> list[dict[str, Any]]:
    metadata_items: list[dict[str, Any]] = []
    for match in re.finditer(r"<chunk_meta>(.*?)</chunk_meta>", llm_result, re.DOTALL):
        body = match.group(1).strip()
        time_match = re.search(r"^\s*时间[：:]\s*(.+)$", body, re.MULTILINE)
        keywords_match = re.search(r"^\s*keywords[：:]\s*(.+)$", body, re.MULTILINE | re.IGNORECASE)
        importance_match = re.search(r"^\s*importance[：:]\s*(.+)$", body, re.MULTILINE | re.IGNORECASE)
        if not time_match or not keywords_match or not importance_match:
            continue
        metadata_items.append(
            {
                "time": time_match.group(1).strip(),
                "keywords": _normalize_keywords(keywords_match.group(1).strip()),
                "importance": parse_event_importance(
                    f"- **重要度**：{importance_match.group(1).strip()}",
                    default=3,
                ),
            }
        )
    return metadata_items


def _merge_chunk_metadata(blocks: list[dict[str, str]], metadata_items: list[dict[str, Any]]) -> list[dict[str, str]]:
    pending: dict[str, list[dict[str, Any]]] = {}
    for item in metadata_items:
        time_key = str(item.get("time", "")).strip()
        if time_key:
            pending.setdefault(time_key, []).append(item)

    merged: list[dict[str, str]] = []
    for block in blocks:
        time_key = extract_event_field(block["content"], "时间")
        item = pending.get(time_key, []).pop(0) if pending.get(time_key) else None
        merged.append(
            _make_block(
                block["date"],
                _apply_chunk_metadata(
                    block["content"],
                    str(item.get("keywords", "")).strip() if item else extract_event_field(block["content"], "关键词"),
                    int(item.get("importance", 3)) if item else parse_event_importance(block["content"], default=3),
                ),
            )
        )
    return merged


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
    return path, original_content, _flatten_sections(sections)




class MemoryConsolidator:
    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _supports_growth(agent_name: str) -> bool:
        return agent_name != "narrator"

    def _get_lock(self, name: str) -> asyncio.Lock:
        if name not in self._locks:
            self._locks[name] = asyncio.Lock()
        return self._locks[name]

    async def _call_step(
        self,
        client: OpenAICompatibleClient,
        agent_name: str,
        step_name: str,
        system: str,
        user: str,
    ) -> str:
        resp = await client.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            enable_thinking=False,
        )
        content = (resp.get("content") or "").strip()
        log_consolidation_call(
            agent_name,
            step_name,
            f"[system]\n{system}\n\n[user]\n{user}",
            content,
            resp.get("usage"),
        )
        return content

    async def _run_step1(
        self,
        client: OpenAICompatibleClient,
        agent_name: str,
        memory_entries: str,
        raw_dialogue: str,
        window_dates: list[str],
    ) -> tuple[list[dict[str, str]], str]:
        system = load_text(_PROMPT_STEP1_PATH)
        user = build_step1_user_payload(agent_name, memory_entries, raw_dialogue)
        result = await self._call_step(client, agent_name, "step1_merge", system, user)
        if len(result) < 50:
            raise ValueError("第一步返回过短，跳过整理")
        sections = parse_llm_memory_sections(result, window_dates)
        validation_error = _validate_step1_result(sections, expected_dates=window_dates)
        if validation_error:
            raise ValueError(validation_error)
        blocks = _flatten_sections(sections)
        return blocks, render_sections(_group_blocks(blocks))

    async def _run_step1_5(
        self,
        client: OpenAICompatibleClient,
        agent_name: str,
        blocks: list[dict[str, str]],
        step1_markdown: str,
    ) -> list[dict[str, str]]:
        system, user = self._build_consolidation_prompt_step1_5(step1_markdown)
        try:
            result = await self._call_step(client, agent_name, "step1_5_chunk_meta", system, user)
            return _merge_chunk_metadata(blocks, _parse_step1_5_metadata(result))
        except Exception as e:
            routing_logger.error(f"[整理器] {agent_name} 第一步半调用失败: {e}")
            return _apply_default_chunk_metadata(blocks)

    async def _run_step2_growth(
        self,
        client: OpenAICompatibleClient,
        agent_name: str,
        step1_markdown: str,
    ) -> None:
        system, user = self._build_consolidation_prompt_step2(agent_name, step1_markdown)
        result = await self._call_step(client, agent_name, "step2_growth", system, user)
        updates = self._parse_step2_growth(result)
        if updates:
            routing_logger.info(f"[整理器] {agent_name} growth.md: {self._apply_growth_updates(agent_name, updates)}")
        else:
            routing_logger.info(f"[整理器] {agent_name} 无人格沉淀更新")

    async def _run_step3_dedup(
        self,
        client: OpenAICompatibleClient,
        agent_name: str,
    ) -> None:
        current_count = len(read_growth_entries(agent_name))
        if current_count <= GROWTH_DEDUP_THRESHOLD:
            routing_logger.info(
                f"[整理器] {agent_name} 跳过第三步去重合并（当前 {current_count} 条，未超过阈值 {GROWTH_DEDUP_THRESHOLD}）"
            )
            return
        routing_logger.info(
            f"[整理器] {agent_name} 触发第三步去重合并（当前 {current_count} 条，阈值 {GROWTH_DEDUP_THRESHOLD}）"
        )
        system, user = self._build_consolidation_prompt_step3(agent_name)
        result = await self._call_step(client, agent_name, "step3_dedup", system, user)
        self._apply_step3_growth(agent_name, result)

    def _build_consolidation_prompt_step2(self, agent_name: str, step1_result: str) -> tuple[str, str]:
        soul_content = read_agent_file(agent_name, "soul.md")
        growth_content = read_agent_file(agent_name, "growth.md") or "（尚无）"
        system = load_text(_PROMPT_STEP2_PATH)
        user = (
            f"<soul>\n{soul_content}\n</soul>\n\n"
            f"<existing_growth>\n{growth_content}\n</existing_growth>\n\n"
            f"<consolidated_memory>\n{step1_result}\n</consolidated_memory>"
        )
        return system, user

    def _build_consolidation_prompt_step1_5(self, step1_markdown: str) -> tuple[str, str]:
        system = load_text(_PROMPT_STEP1_5_PATH)
        user = f"<consolidated_memory>\n{step1_markdown}\n</consolidated_memory>"
        return system, user

    def _build_consolidation_prompt_step3(self, agent_name: str) -> tuple[str, str]:
        growth_content = read_agent_file(agent_name, "growth.md") or "（尚无）"
        return load_text(_PROMPT_STEP3_PATH), f"<existing_growth>\n{growth_content}\n</existing_growth>"

    def _apply_step3_growth(self, agent_name: str, llm_result: str) -> None:
        match = re.search(r"<merged_growth>(.*?)</merged_growth>", llm_result, re.DOTALL)
        if not match:
            routing_logger.warning(f"[整理器] {agent_name} 第三步未找到 <merged_growth> 标签，跳过")
            return

        entries: dict[str, str] = {}
        for line in match.group(1).strip().splitlines():
            line = line.strip()
            item_match = re.match(r"\[(P\d+)\]\s*(.*)", line)
            if item_match:
                entries[item_match.group(1)] = item_match.group(2).strip()
        if not entries:
            routing_logger.warning(f"[整理器] {agent_name} 第三步解析结果为空，跳过")
            return

        growth_path = Path(character_path(agent_name, "growth.md"))
        if growth_path.exists():
            backup_file(growth_path, agent_name, "growth")

        write_growth_entries(
            agent_name,
            {f"P{i:03d}": content for i, content in enumerate(entries.values(), start=1)},
        )
        routing_logger.info(f"[整理器] {agent_name} 第三步合并完成，条目数: {len(entries)}")

    async def _run_memory_pipeline(
        self,
        agent_name: str,
        memory_entries: str,
        window_dates: list[str],
        raw_dialogue: str = "",
    ) -> tuple[list[dict[str, str]] | None, list[str]]:
        errors: list[str] = []

        async with OpenAICompatibleClient(
            **get_consolidation_llm_config(temperature=CONSOLIDATION_TEMPERATURE),
            max_tokens=CONSOLIDATION_MAX_TOKENS,
            timeout=120.0,
            max_retries=3,
        ) as client:
            try:
                blocks, step1_markdown = await self._run_step1(
                    client, agent_name, memory_entries, raw_dialogue, window_dates
                )
            except Exception as e:
                errors.append(f"第一步调用失败: {e}")
                routing_logger.error(f"[整理器] {agent_name} 第一步调用失败: {e}")
                return None, errors

            blocks = await self._run_step1_5(client, agent_name, blocks, step1_markdown)

            if not self._supports_growth(agent_name):
                routing_logger.info(f"[整理器] {agent_name} 跳过 growth.md 流程")
                return blocks, errors

            try:
                await self._run_step2_growth(client, agent_name, step1_markdown)
            except Exception as e:
                errors.append(f"第二步调用失败: {e}")
                routing_logger.error(f"[整理器] {agent_name} 第二步调用失败: {e}")
                return blocks, errors

            try:
                await self._run_step3_dedup(client, agent_name)
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

        # 从前往后找第一个未整理块；若全文都已整理，则将最后一块纳入窗口重整理。
        first_unstructured_index = next((i for i, block in enumerate(blocks) if not _is_structured_block(block)), None)
        start_index = first_unstructured_index if first_unstructured_index is not None else len(blocks) - 1

        stable_blocks = blocks[:start_index]
        window_blocks = blocks[start_index:]
        window_dates = list(OrderedDict.fromkeys(block["date"] for block in window_blocks))
        raw_dialogue = format_raw_dialogue_for_owner(agent_name, RAW_DIALOGUE_LIMIT)
        if not raw_dialogue:
            return None, f"最近 {RAW_DIALOGUE_LIMIT} 条消息中无参与"
        return _ConsolidationWindow(
            stable_blocks=stable_blocks,
            window_blocks=window_blocks,
            window_dates=window_dates,
            window_memory_entries=render_sections(_group_blocks(window_blocks)),
            raw_dialogue=raw_dialogue,
        ), None

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
                rewritten_blocks, pipeline_errors = await self._run_memory_pipeline(
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

            merged_sections = _group_blocks(merged_blocks)
            result.final_len, consolidated_len = safe_write_memory(path, merged_sections, agent_name, original_content)
            if result.final_len < 0:
                result.errors.append("并发冲突：检测到中间变更，已放弃写回")
                return result

            actual_window_blocks = merged_blocks[len(window.stable_blocks):]
            update_dates = set(window.window_dates) | {block["date"] for block in actual_window_blocks}
            for date in update_dates:
                await vector_store.add(agent_name, date, split_into_events(merged_sections.get(date, "")))

            user_before, user_after = await self._consolidate_player_profile(agent_name)
            result.user_md_before, result.user_md_after = user_before, user_after
            return result

    def _parse_step2_growth(self, llm_result: str) -> list[dict]:
        match = re.search(r"<personality_updates>(.*?)</personality_updates>", llm_result, re.DOTALL)
        if not match or not match.group(1).strip():
            return []

        raw_updates = match.group(1).strip()
        updates: list[dict[str, str]] = []
        tagged_matches = list(re.finditer(r"<update\b[^>]*>(.*?)</update>", raw_updates, re.DOTALL))
        if tagged_matches:
            for item in tagged_matches:
                content = item.group(1).strip() if item.group(1) else ""
                if content:
                    updates.append({"content": content})
            return updates

        for line in raw_updates.splitlines():
            content = line.strip()
            if content:
                updates.append({"content": content})
        return updates

    def _apply_growth_updates(self, agent_name: str, updates: list[dict]) -> str:
        entries = read_growth_entries(agent_name)
        logs: list[str] = []
        next_index = max(
            (int(m.group(1)) for key in entries if (m := re.fullmatch(r"P(\d{3})", key))),
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

    async def _consolidate_player_profile(self, agent_name: str) -> tuple[int, int]:
        user_path = Path(character_path(agent_name, "user.md"))
        tmp_path = Path(character_path(agent_name, "tmp_user.md"))

        if not user_path.exists():
            return 0, 0

        user_content = user_path.read_text(encoding="utf-8")
        tmp_content = tmp_path.read_text(encoding="utf-8").strip() if tmp_path.exists() else ""

        # 没有工作草稿需要整理，跳过
        if not tmp_content:
            return 0, 0

        draft_input = _build_player_profile_draft_input(tmp_content)

        try:
            backup_file(user_path, agent_name, "user")
            system = load_text(_PLAYER_PROMPT_PATH).format(fields_definition=build_fields_definition(agent_name))
            async with OpenAICompatibleClient(
                **get_consolidation_llm_config(temperature=CONSOLIDATION_TEMPERATURE),
                timeout=120.0,
                max_retries=3,
            ) as client:
                resp = await client.chat(
                    [{"role": "system", "content": system}, {"role": "user", "content": draft_input}],
                    enable_thinking=False,
                )
            consolidated = (resp.get("content") or "").strip()
            if len(consolidated.strip()) < 20:
                routing_logger.warning(f"[整理器] {agent_name} user.md LLM 返回过短，跳过")
                return 0, 0

            diary_match = re.search(r"^.*第二步.*档案.*$", consolidated, re.MULTILINE)
            if diary_match:
                consolidated = consolidated[diary_match.end():].lstrip("\n")
            consolidated = _enforce_user_section_limits(consolidated)

            user_path.write_text(consolidated.strip() + "\n", encoding="utf-8")
            tmp_path.unlink(missing_ok=True)
            before_len, after_len = len(user_content), len(consolidated)
            routing_logger.info(f"[整理器] {agent_name} user.md 整理完成 (长度: {before_len} → {after_len})")
            return before_len, after_len
        except Exception as e:
            routing_logger.error(f"[整理器] {agent_name} user.md 整理失败: {e}")
            return 0, 0

    async def consolidate_all(self, agent_names: list[str]):
        t0 = time.monotonic()
        agent_names = [name for name in agent_names if name != "narrator"]
        if not agent_names:
            routing_logger.info("[整理器] 无需整理：当前列表中没有可整理角色")
            return

        summaries: list[str] = []
        for name in agent_names:
            path = Path(character_path(name, "memory.md"))
            summaries.append(f"{name}.memory({len(path.read_text(encoding='utf-8'))}字)" if path.exists() else f"{name}(无文件)")
        routing_logger.info(f"[整理器] 开始记忆整理: {', '.join(summaries)}")

        raw_results = await asyncio.gather(*(self.consolidate_agent(name) for name in agent_names), return_exceptions=True)
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
                f"{item.original_len}→{item.final_len}字({(item.final_len - item.original_len) / item.original_len * 100:+.1f}%)"
                if item.original_len > 0 else "无变化"
            )
            user_part = f" | user.md {item.user_md_before}→{item.user_md_after}" if item.user_md_before > 0 else ""
            err_part = f" | 错误: {', '.join(item.errors)}" if item.errors else ""
            routing_logger.info(
                f"[整理器] {item.agent_name} 完成: {item.days}天({item.date_range}) {mem_part}{user_part}{err_part}"
            )

        routing_logger.info(f"[整理器] 全部完成 (耗时 {time.monotonic() - t0:.1f}s)")


memory_consolidator = MemoryConsolidator()
