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
from memory.file_ops import (
    _get_fields_from_file,
    backup_file,
    extract_event_field,
    load_text,
    normalize,
    parse_event_importance,
    parse_event_keywords,
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
_PROMPT_STEP1_5_PATH = Path(__file__).parent.parent / "prompts" / "memory_chunk_metadata.txt"
_PROMPT_STEP2_PATH = Path(__file__).parent.parent / "prompts" / "growth_extract.txt"
_PROMPT_STEP3_PATH = Path(__file__).parent.parent / "prompts" / "growth_dedupe.txt"
_PLAYER_PROMPT_PATH = (
    Path(__file__).parent.parent / "prompts" / "player_profile_consolidation_prompt.txt"
)
_REQUIRED_EVENT_FIELDS = ["**时间**", "**地点**", "**在场**", "**内容**"]

_USER_FIELD_DESCRIPTIONS: dict[str, str] = {
    "基本信息": "优先保留已确认的客观信息：姓名、年龄、性别、身份",
    "他是什么人": "最多 8 条：跨情境成立的性格、习惯、边界方式与行事风格（主语是\"他\"），不要重复基本信息",
    "我们怎么相处": "最多 5 条：我和他之间反复出现的双向互动规律（主语是\"我们/我和他\"）",
}


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
    fields = _get_fields_from_file(file_path) or ["基本信息", "他是什么人", "我们怎么相处"]
    return "\n".join(f"- 「{field}」：{_USER_FIELD_DESCRIPTIONS.get(field, '')}" for field in fields)


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


def _render_sections(sections: OrderedDict[str, str]) -> str:
    parts: list[str] = []
    for date, body in sections.items():
        parts.append(f"## {date}")
        parts.append(body.strip())
        parts.append("")
    return "\n".join(parts).strip()


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
                parse_event_keywords(block["content"]),
                parse_event_importance(block["content"], default=3),
            ),
        )
        for block in blocks
    ]


def _validate_step1_result(expected_dates: list[str], sections: OrderedDict[str, str]) -> str | None:
    actual_dates = list(sections.keys())
    if actual_dates != expected_dates:
        expected = ", ".join(expected_dates) or "（空）"
        actual = ", ".join(actual_dates) or "（空）"
        routing_logger.warning(
            "[整理器] step1 返回日期与窗口不一致: expected=%s, actual=%s",
            expected,
            actual,
        )
        return f"第一步返回日期与窗口不一致(expected={expected}; actual={actual})"

    missing_map = [
        f"{block['date']} 字段不完整({','.join(missing)})"
        for block in _flatten_sections(sections)
        if (missing := [field for field in _REQUIRED_EVENT_FIELDS if field not in block["content"]])
    ]
    if missing_map:
        return missing_map[0] + "，已跳过"
    return None


def _parse_step1_memories(llm_result: str, expected_dates: list[str] | None = None) -> OrderedDict[str, str]:
    cleaned = re.sub(r"<analysis>.*?</analysis>", "", llm_result, flags=re.DOTALL).strip()
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
            sections[current_date] = sections.get(current_date, "") + ("\n\n" if current_date in sections else "") + event_text
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
            current_date = explicit_date_match.group(1) if explicit_date_match else current_heading_date or fallback_date
            if explicit_date_match:
                current_lines = [f"- **时间**：{time_value}"]
            elif current_date:
                current_lines = [f"- **时间**：{current_date} {time_value}".rstrip()]
            else:
                current_lines = [f"- **时间**：{time_value}"]
            continue

        if current_lines:
            field_match = field_pattern.match(line)
            if field_match:
                current_lines.append(f"- **{field_match.group(1)}**：{field_match.group(2).strip()}".rstrip())
            else:
                current_lines.append(line)

    flush_event()
    return sections


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
                    str(item.get("keywords", "")).strip() if item else parse_event_keywords(block["content"]),
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


def _resolve_window_start(agent_name: str, blocks: list[dict[str, str]], original_content: str, cdata: dict) -> tuple[int | None, str | None]:
    if not blocks:
        return None, "memory 中没有可整理的块"

    stored_fingerprint = cdata.get("last_consolidated_block_id")
    anchor_fingerprint: str | None = None
    boundary_invalid = False
    if stored_fingerprint:
        if any(block["fingerprint"] == stored_fingerprint for block in blocks):
            anchor_fingerprint = stored_fingerprint
        else:
            routing_logger.warning("[整理器] %s 已整理块标识失效，尝试用 last_memory_size 恢复边界", agent_name)

    last_memory_size = cdata.get("last_memory_size")
    if anchor_fingerprint is None and isinstance(last_memory_size, int) and 0 < last_memory_size <= len(original_content):
        snapshot_content = normalize(original_content[:last_memory_size])
        snapshot_sections = split_by_date(snapshot_content)
        snapshot_blocks = _flatten_sections(snapshot_sections)
        if snapshot_blocks:
            recovered = snapshot_blocks[-1]["fingerprint"]
            if any(block["fingerprint"] == recovered for block in blocks):
                if stored_fingerprint and stored_fingerprint != recovered:
                    routing_logger.info(
                        "[整理器] %s 用 last_memory_size 恢复已整理块边界: %s -> %s",
                        agent_name,
                        stored_fingerprint[:10],
                        recovered[:10],
                    )
                anchor_fingerprint = recovered
            else:
                boundary_invalid = True
    elif anchor_fingerprint is None and cdata:
        boundary_invalid = True

    if anchor_fingerprint is not None:
        start_index = next(i for i, block in enumerate(blocks) if block["fingerprint"] == anchor_fingerprint)
        migrated_anchor = bool(
            stored_fingerprint
            and stored_fingerprint != anchor_fingerprint
            and not any(block["fingerprint"] == stored_fingerprint for block in blocks)
        )
        if not migrated_anchor and isinstance(last_memory_size, int) and len(original_content) <= last_memory_size:
            return None, "memory 未增长，跳过本轮整理"
        return start_index, None

    has_growth = not isinstance(last_memory_size, int) or len(original_content) > last_memory_size
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

    return 0, None


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
        rewritten_blocks: list[dict[str, str]] | None = None

        async with OpenAICompatibleClient(
            **get_consolidation_llm_config(temperature=CONSOLIDATION_TEMPERATURE),
            max_tokens=CONSOLIDATION_MAX_TOKENS,
            timeout=120.0,
            max_retries=3,
        ) as client:
            system_step1 = load_text(_PROMPT_STEP1_PATH)
            user_step1 = build_step1_user_payload(agent_name, memory_entries, raw_dialogue)
            try:
                resp1 = await client.chat(
                    [{"role": "system", "content": system_step1}, {"role": "user", "content": user_step1}],
                    enable_thinking=False,
                )
                step1_result = (resp1.get("content") or "").strip()
                log_consolidation_call(
                    agent_name,
                    "step1_merge",
                    f"[system]\n{system_step1}\n\n[user]\n{user_step1}",
                    step1_result,
                    resp1.get("usage"),
                )
            except Exception as e:
                errors.append(f"第一步调用失败: {e}")
                routing_logger.error(f"[整理器] {agent_name} 第一步调用失败: {e}")
                return None, errors

            if len(step1_result) < 50:
                return None, [*errors, "第一步返回过短，跳过整理"]

            step1_sections = _parse_step1_memories(step1_result, window_dates)
            validation_error = _validate_step1_result(window_dates, step1_sections)
            if validation_error:
                return None, [*errors, validation_error]

            rewritten_blocks = _flatten_sections(step1_sections)
            step1_markdown = _render_sections(_group_blocks(rewritten_blocks))

            system_step1_5, user_step1_5 = self._build_consolidation_prompt_step1_5(step1_markdown)
            try:
                resp1_5 = await client.chat(
                    [{"role": "system", "content": system_step1_5}, {"role": "user", "content": user_step1_5}],
                    enable_thinking=False,
                )
                step1_5_result = (resp1_5.get("content") or "").strip()
                log_consolidation_call(
                    agent_name,
                    "step1_5_chunk_meta",
                    f"[system]\n{system_step1_5}\n\n[user]\n{user_step1_5}",
                    step1_5_result,
                    resp1_5.get("usage"),
                )
                rewritten_blocks = _merge_chunk_metadata(rewritten_blocks, _parse_step1_5_metadata(step1_5_result))
            except Exception as e:
                errors.append(f"第一点五步调用失败: {e}")
                routing_logger.error(f"[整理器] {agent_name} 第一步半调用失败: {e}")
                rewritten_blocks = _apply_default_chunk_metadata(rewritten_blocks)

            if not self._supports_growth(agent_name):
                routing_logger.info(f"[整理器] {agent_name} 跳过 growth.md 流程")
                return rewritten_blocks, errors

            system_step2, user_step2 = self._build_consolidation_prompt_step2(agent_name, step1_markdown)
            try:
                resp2 = await client.chat(
                    [{"role": "system", "content": system_step2}, {"role": "user", "content": user_step2}],
                    enable_thinking=False,
                )
                step2_result = (resp2.get("content") or "").strip()
                log_consolidation_call(
                    agent_name,
                    "step2_growth",
                    f"[system]\n{system_step2}\n\n[user]\n{user_step2}",
                    step2_result,
                    resp2.get("usage"),
                )
            except Exception as e:
                errors.append(f"第二步调用失败: {e}")
                routing_logger.error(f"[整理器] {agent_name} 第二步调用失败: {e}")
                return rewritten_blocks, errors

            step2_updates = self._parse_step2_growth(step2_result)
            if step2_updates:
                routing_logger.info(f"[整理器] {agent_name} growth.md: {self._apply_growth_updates(agent_name, step2_updates)}")
            else:
                routing_logger.info(f"[整理器] {agent_name} 无人格沉淀更新")

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
                        agent_name,
                        "step3_dedup",
                        f"[system]\n{system_step3}\n\n[user]\n{user_step3}",
                        step3_result,
                        resp3.get("usage"),
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
            cdata = read_sidecar_json(agent_name, ".consolidation_state.json")
            start_index, skip_reason = _resolve_window_start(agent_name, blocks, original_content, cdata)
            if start_index is None:
                if skip_reason:
                    result.skipped, result.skip_reason = True, skip_reason
                    routing_logger.info(f"[整理器] {agent_name} 跳过: {skip_reason}")
                return result if result.skipped else None

            stable_blocks = blocks[:start_index]
            window_blocks = blocks[start_index:]
            window_dates = list(OrderedDict.fromkeys(block["date"] for block in window_blocks))
            window_memory_entries = _render_sections(_group_blocks(window_blocks))

            raw_dialogue = format_raw_dialogue_for_owner(agent_name, RAW_DIALOGUE_LIMIT)
            if not raw_dialogue:
                result.skipped, result.skip_reason = True, f"最近 {RAW_DIALOGUE_LIMIT} 条消息中无参与"
                routing_logger.info(f"[整理器] {agent_name} 跳过: {result.skip_reason}")
                return result

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

            merged_sections = _group_blocks(merged_blocks)
            result.final_len, consolidated_len = safe_write_memory(path, merged_sections, agent_name, original_content)
            if result.final_len < 0:
                result.errors.append("并发冲突：检测到中间变更，已放弃写回")
                return result

            for date in window_dates:
                await vector_store.add(agent_name, date, split_into_events(merged_sections.get(date, "")))

            if not result.errors:
                cdata.update(
                    last_consolidated_date=merged_blocks[-1]["date"],
                    last_consolidated_block_id=merged_blocks[-1]["fingerprint"],
                    last_memory_size=consolidated_len,
                )
                write_sidecar_json(agent_name, ".consolidation_state.json", cdata)

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
        if not user_path.exists():
            return 0, 0

        content = user_path.read_text(encoding="utf-8")
        if len(content.strip()) < 100:
            return 0, 0

        try:
            backup_file(user_path, agent_name, "user")
            system = load_text(_PLAYER_PROMPT_PATH).format(fields_definition=build_fields_definition(agent_name))
            async with OpenAICompatibleClient(
                **get_consolidation_llm_config(temperature=CONSOLIDATION_TEMPERATURE),
                timeout=120.0,
                max_retries=3,
            ) as client:
                resp = await client.chat(
                    [{"role": "system", "content": system}, {"role": "user", "content": content}],
                    enable_thinking=False,
                )
            consolidated = (resp.get("content") or "").strip()
            if len(consolidated.strip()) < 20:
                routing_logger.warning(f"[整理器] {agent_name} user.md LLM 返回过短，跳过")
                return 0, 0

            diary_match = re.search(r"^.*第二步.*档案.*$", consolidated, re.MULTILINE)
            if diary_match:
                consolidated = consolidated[diary_match.end():].lstrip("\n")

            user_path.write_text(consolidated.strip() + "\n", encoding="utf-8")
            return len(content), len(consolidated)
        except Exception as e:
            routing_logger.error(f"[整理器] {agent_name} user.md 整理失败: {e}")
            return 0, 0

    async def consolidate_player_profile(self, agent_name: str):
        before, after = await self._consolidate_player_profile(agent_name)
        if before > 0:
            routing_logger.info(f"[整理器] {agent_name} user.md 整理完成 (长度: {before} → {after})")

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
