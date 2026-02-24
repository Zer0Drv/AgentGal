"""后台记忆整理器 - 每个日期都过 LLM 整合 + 增量进度

normalize → split_by_date（合并同日期段落）→ 根据进度跳过已整合日期

进度机制：记录上次整合到哪个日期，下次从该日期开始，避免过度压缩旧记忆。
"""

import asyncio
import json
import os
import re
import shutil
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from functools import lru_cache

from llm.llm_parser import OpenAICompatibleClient

from log_config.routing import routing_logger
from engine.config import character_path
from memory.vector_store import vector_store
from memory.text_utils import (
    normalize,
    split_by_date,
    split_events_raw,
    split_into_events,
)

CONSOLIDATION_INTERVAL = int(os.getenv("CONSOLIDATION_INTERVAL", "10"))

# 从统一配置读取，兼容旧版 DEEPSEEK_API_KEY
_API_KEY = os.getenv("CONSOLIDATION_LLM_API_KEY") or os.getenv("LLM_API_KEY")
_API_URL = (
    os.getenv("CONSOLIDATION_LLM_API_URL")
    or os.getenv("LLM_API_URL")
    or "https://api.deepseek.com/v1"
)
_MODEL_ID = (
    os.getenv("CONSOLIDATION_LLM_MODEL_ID")
    or os.getenv("LLM_MODEL_ID")
    or "deepseek-chat"
)
# 整理用较低 temperature，保证输出稳定
_TEMPERATURE = float(os.getenv("CONSOLIDATION_TEMPERATURE", "0.0"))
_MAX_TOKENS = int(os.getenv("CONSOLIDATION_MAX_TOKENS", "8192"))

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "consolidation_prompt.txt"
_PLAYER_PROMPT_PATH = (
    Path(__file__).parent.parent / "prompts" / "player_profile_consolidation_prompt.txt"
)


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

@lru_cache(maxsize=4)
def _load_text(path: Path) -> str:
    """小型只读缓存，避免重复读文件。"""
    return path.read_text(encoding="utf-8")


def _cleanup_old_backups(bak_dir: Path, pattern: str, max_count: int = 10) -> int:
    """清理旧备份文件，只保留最近的 max_count 个。

    Args:
        bak_dir: 备份目录路径
        pattern: 文件匹配模式，如 "Memory_*_pre.md"
        max_count: 最大保留数量，默认 10

    Returns:
        删除的文件数量
    """
    bak_files = sorted(
        bak_dir.glob(pattern),
        key=lambda f: f.stat().st_mtime,
    )
    deleted = 0
    if len(bak_files) > max_count:
        for old_bak in bak_files[:-max_count]:
            old_bak.unlink()
            deleted += 1
    return deleted


class MemoryConsolidator:
    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        # 统一走项目 OpenAI 兼容客户端，避免重复实现 HTTP 细节
        self._client: Optional[OpenAICompatibleClient] = None

    def _get_lock(self, name: str) -> asyncio.Lock:
        if name not in self._locks:
            self._locks[name] = asyncio.Lock()
        return self._locks[name]

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM 进行记忆整理（使用统一客户端满足 DRY/KISS）。"""
        if not _API_KEY:
            raise ValueError(
                "LLM API key not configured. Please set LLM_API_KEY or CONSOLIDATION_LLM_API_KEY"
            )

        # 懒加载客户端；OpenAICompatibleClient 负责 /chat/completions 与重试逻辑
        if self._client is None:
            self._client = OpenAICompatibleClient(
                api_url=_API_URL,
                api_key=_API_KEY,
                model=_MODEL_ID,
                temperature=_TEMPERATURE,
                max_tokens=_MAX_TOKENS,
                timeout=120.0,
                max_retries=3,
            )
            await self._client.initialize()

        resp = await self._client.chat(
            messages=[{"role": "user", "content": prompt}],
            enable_thinking=False,  # 整理稳定性优先
        )
        return (resp.get("content") or "").strip()

    def _backup_file(self, src: Path, agent_name: str, prefix: str) -> Path:
        """
        备份文件到 agent 的 bak 目录，保留最近 10 个备份。

        Returns:
            备份文件的完整路径
        """
        bak_dir = Path(character_path(agent_name, "bak"))
        bak_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        bak_path = bak_dir / f"{prefix}_{ts}_pre.md"
        shutil.copy2(src, bak_path)

        _cleanup_old_backups(bak_dir, f"{prefix}_*_pre.md", max_count=10)
        return bak_path

    def _state_path(self, agent_name: str) -> Path:
        """整合进度文件路径"""
        return Path(character_path(agent_name, ".consolidation_state.json"))

    def _load_state(self, agent_name: str) -> Optional[str]:
        """读取上次整合到的日期，返回如 '2月10日' 或 None"""
        p = self._state_path(agent_name)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data.get("last_consolidated_date")
        except (json.JSONDecodeError, OSError):
            return None

    def _save_state(self, agent_name: str, last_date: str):
        """保存整合进度"""
        p = self._state_path(agent_name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"last_consolidated_date": last_date}, ensure_ascii=False),
            encoding="utf-8",
        )

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

    def _resolve_dates(
        self, agent_name: str, all_dates: list[str], last_consolidated: str | None
    ) -> tuple[list[str], str | None] | None:
        """
        根据进度确定需要整合的日期范围。

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
            new_dates = all_dates[current_idx + 1 :]
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

    def _build_consolidation_prompt(
        self, agent_name: str, sections: OrderedDict[str, str], dates: list[str]
    ) -> str:
        """构建记忆整合的 LLM prompt。"""
        soul_path = Path(character_path(agent_name, "soul.md"))
        soul_content = (
            soul_path.read_text(encoding="utf-8") if soul_path.exists() else ""
        )

        growth_path = Path(character_path(agent_name, "growth.md"))
        growth_content = (
            growth_path.read_text(encoding="utf-8")
            if growth_path.exists()
            else "（尚无）"
        )

        parts = [f"## {date}\n{sections[date]}" for date in dates]
        combined_text = "\n\n".join(parts)

        template = _load_text(_PROMPT_PATH)
        return template.format(
            soul=soul_content,
            growth=growth_content,
            content=combined_text,
        )

    def _apply_memory_result(
        self,
        agent_name: str,
        sections: OrderedDict[str, str],
        dates: list[str],
        llm_result: str,
        result: "_ConsolidationResult",
    ):
        """解析 LLM 结果并应用更新到 sections。"""
        if len(llm_result.strip()) < 50:
            result.errors.append("LLM返回过短，跳过整理")
            return

        # ===== 第一步：更新 memory.md =====
        step1_sections = self._parse_step1_memories(llm_result)
        if step1_sections:
            for date in dates:
                if date in step1_sections:
                    sections[date] = step1_sections[date]
                else:
                    result.errors.append(f"{date} 未在LLM返回中找到")
        else:
            result.errors.append("未能解析第一步:归并整理")

        # ===== 第二步：更新 growth.md =====
        step2_updates = self._parse_step2_growth(llm_result)
        if step2_updates:
            growth_log = self._apply_growth_updates(agent_name, step2_updates)
            routing_logger.info(f"[整理器] {agent_name} growth.md: {growth_log}")
        else:
            routing_logger.info(f"[整理器] {agent_name} 无人格沉淀更新")

    async def consolidate_agent(
        self, agent_name: str
    ) -> Optional["_ConsolidationResult"]:
        """整理单个 agent 的记忆，返回结果摘要（供 consolidate_all 汇总日志）"""
        result = _ConsolidationResult(agent_name=agent_name)
        lock = self._get_lock(agent_name)
        if lock.locked():
            result.skipped = True
            result.skip_reason = "已有整理任务在运行"
            return result

        async with lock:
            # 1. 加载文件并 normalize
            loaded = self._load_and_normalize(agent_name)
            if loaded is None:
                return None
            path, original_content, sections = loaded

            # 2. 确定需要整合的日期
            all_dates = list(sections.keys())
            last_consolidated = self._load_state(agent_name)
            resolved = self._resolve_dates(agent_name, all_dates, last_consolidated)
            if resolved is None:
                return None
            dates_to_consolidate, next_date = resolved

            if not dates_to_consolidate:
                return None

            result.days = len(dates_to_consolidate)
            result.date_range = f"{dates_to_consolidate[0]}~{dates_to_consolidate[-1]}"
            result.original_len = len(original_content)

            # 3. 备份
            self._backup_file(path, agent_name, "Memory")

            # 4. 构建 prompt 并调用 LLM
            prompt = self._build_consolidation_prompt(
                agent_name, sections, dates_to_consolidate
            )
            try:
                llm_result = await self._call_llm(prompt)
                self._apply_memory_result(
                    agent_name, sections, dates_to_consolidate, llm_result, result
                )
            except Exception as e:
                result.errors.append(f"整合失败: {e}")
                routing_logger.error(f"[整理器] {agent_name} 整合失败: {e}")

            # 5. 写回文件（检测并发冲突）
            result.final_len = self._write_back(
                path, agent_name, sections, original_content
            )
            if result.final_len < 0:
                result.errors.append("并发冲突：检测到中间变更，已放弃写回")
                return result

            # 6. 同步到向量存储（取消）
            # 新策略：不再将整理后的事件片段写入向量库，仅保留整轮原始对话写入。
            # 因此这里不再做任何向量写入操作。

            # 7. 更新进度
            if next_date:
                if last_consolidated and next_date != last_consolidated:
                    routing_logger.info(
                        f"[整理器] {agent_name} 进度推进: {last_consolidated} → {next_date}"
                    )
                self._save_state(agent_name, next_date)

            # 8. 顺带整理 user.md
            user_before, user_after = await self._consolidate_player_profile(agent_name)
            result.user_md_before = user_before
            result.user_md_after = user_after

            return result

    # ===== 双解析器：第一步 + 第二步 =====

    def _parse_step1_memories(self, llm_result: str) -> OrderedDict[str, str]:
        """
        从 LLM 输出中提取第一步：归并整理后的日记内容。

        新格式（扁平列表，日期在时间字段中）：
        ## 第一步：归并整理
        - **时间**：4月3日 上午
        - **地点**：教室
        - **在场**：莉莉丝、李小明
        - **内容**：事件描述...

        Returns:
            OrderedDict[日期, 该日期的内容]
        """
        # 提取 "## 第一步" 到 "## 第二步" 之间的内容
        step1_pattern = r"##\s*第一步.*?(?:##\s*第二步|$)"
        step1_match = re.search(step1_pattern, llm_result, re.DOTALL)

        if not step1_match:
            return OrderedDict()

        step1_content = step1_match.group(0)
        # 移除第一步标题本身
        step1_content = re.sub(r"^##\s*第一步.*\n", "", step1_content, count=1)
        # 移除第二步标记（如果有）
        step1_content = re.sub(r"##\s*第二步.*$", "", step1_content, flags=re.DOTALL)

        # 解析新格式：从 - **时间**：字段中提取日期
        sections: OrderedDict[str, str] = OrderedDict()
        for date, event_text in split_events_raw(step1_content.strip()):
            if not date:
                continue
            sections[date] = (sections.get(date, "") + ("\n\n" if date in sections else "") + event_text)
        return sections

    def _parse_step2_growth(self, llm_result: str) -> list[dict]:
        """
        从 LLM 输出中提取第二步：人格沉淀更新。

        Returns:
            [{"type": "ADD|UPDATE|DELETE", "id": "P001", "content": "..."}]
        """
        # 提取 personality_updates 部分
        pattern = r"<personality_updates>(.*?)</personality_updates>"
        match = re.search(pattern, llm_result, re.DOTALL)

        if not match:
            return []

        updates = []
        # 解析每个 update 标签，支持自闭合和内容形式
        update_pattern = r'<update\s+type="(\w+)"\s+id="(\w+)"\s*(?:/>|>(.*?)</update>)'
        for m in re.finditer(update_pattern, match.group(1), re.DOTALL):
            updates.append(
                {
                    "type": m.group(1).upper(),  # ADD/UPDATE/DELETE
                    "id": m.group(2),  # P001
                    "content": m.group(3).strip() if m.group(3) else None,
                }
            )

        return updates

    def _read_growth(self, agent_name: str) -> dict[str, str]:
        """
        读取 growth.md，返回 {id: content} 字典。

        格式：[P001] 内容（支持多行）
        """
        path = Path(character_path(agent_name, "growth.md"))
        if not path.exists():
            return {}

        content = path.read_text(encoding="utf-8")
        entries = {}
        # 匹配 [P001] 内容（支持多行）
        pattern = r"\[(\w+)\]\s*(.+?)(?=\n\[|$)"
        for m in re.finditer(pattern, content, re.DOTALL):
            entries[m.group(1)] = m.group(2).strip()
        return entries

    def _write_growth(self, agent_name: str, entries: dict[str, str]):
        """将 {id: content} 字典写回 growth.md，按 ID 数字部分排序。"""
        path = Path(character_path(agent_name, "growth.md"))

        def _sort_key(k: str) -> int:
            try:
                return int(re.sub(r"[^0-9]", "", k))
            except ValueError:
                return 0

        sorted_ids = sorted(entries.keys(), key=_sort_key)
        body = "\n\n".join(f"[{i}] {entries[i]}\n" for i in sorted_ids)
        text = f"# 人格沉淀层\n\n{body}\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _apply_growth_updates(self, agent_name: str, updates: list[dict]) -> str:
        """
        应用人格沉淀更新到 growth.md

        Returns:
            操作日志字符串
        """
        entries = self._read_growth(agent_name)
        logs = []

        for up in updates:
            if up["type"] == "ADD":
                if up["id"] in entries:
                    logs.append(f"ADD失败:{up['id']}已存在")
                else:
                    entries[up["id"]] = up["content"] or ""
                    logs.append(f"ADD {up['id']}")

            elif up["type"] == "UPDATE":
                if up["id"] not in entries:
                    logs.append(f"UPDATE警告:{up['id']}不存在转为ADD")
                else:
                    logs.append(f"UPDATE {up['id']}")
                entries[up["id"]] = up["content"] or ""

            elif up["type"] == "DELETE":
                if up["id"] in entries:
                    del entries[up["id"]]
                    logs.append(f"DELETE {up['id']}")
                else:
                    logs.append(f"DELETE警告:{up['id']}不存在")

        self._write_growth(agent_name, entries)
        return ";".join(logs) if logs else "无更新"

    def _write_back(
        self,
        path: Path,
        agent_name: str,
        sections: OrderedDict[str, str],
        original_content: str,
    ) -> int:
        """写回整理后的内容，带最小并发保护。

        策略（快速对齐版）：
        - 若 current 以 original 开头，视为尾部追加，保留该追加；
        - 若 current == original，正常覆盖；
        - 否则判定为中间变更，放弃写回并告警，返回 -1（让上层跳过后续流程）。
        """
        current_content = path.read_text(encoding="utf-8")

        if current_content.startswith(original_content):
            appended = current_content[len(original_content) :]
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
            return -1

        parts = [f"# {agent_name} 的长期记忆", ""]
        for date, body in sections.items():
            parts.append(f"## {date}")
            parts.append(body.strip())
            parts.append("")
        result = "\n".join(parts).strip() + "\n"

        # 追加并发期间新写入的内容
        if appended:
            result += appended

        path.write_text(result, encoding="utf-8")
        return len(result)

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
            self._backup_file(user_path, agent_name, "user")

            prompt = _load_text(_PLAYER_PROMPT_PATH).format(content=content)
            consolidated = await self._call_llm(prompt)

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
                consolidated = consolidated[diary_match.end() :].lstrip("\n")

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
                summaries.append(f"{name}({length}字)")
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

            # 构建压缩率
            if r.original_len > 0:
                ratio = (1 - r.final_len / r.original_len) * 100
                mem_part = f"{r.original_len}→{r.final_len}字({ratio:+.1f}%)"
            else:
                mem_part = "无变化"

            # user.md 部分
            user_part = ""
            if r.user_md_before > 0:
                user_part = f" | user.md {r.user_md_before}→{r.user_md_after}"

            # 错误部分
            err_part = ""
            if r.errors:
                err_part = f" | 错误: {', '.join(r.errors)}"

            routing_logger.info(
                f"[整理器] {r.agent_name} 完成: "
                f"{r.days}天({r.date_range}) {mem_part}{user_part}{err_part}"
            )

        elapsed = time.monotonic() - t0
        routing_logger.info(f"[整理器] 全部完成 (耗时 {elapsed:.1f}s)")

    async def close(self):
        if self._client:
            await self._client.close()


memory_consolidator = MemoryConsolidator()
