"""后台记忆整理器 - 每个日期都过 LLM 整合 + 增量进度

normalize → split_by_date（合并同日期段落）→ 根据进度跳过已整合日期
→ 对需要整合的日期调 LLM 整合 → write → 触发向量库全量重建

进度机制：记录上次整合到哪个日期，下次从该日期开始，避免过度压缩旧记忆。
"""

import json
import os
import re
import shutil
import asyncio
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
import httpx
from .routing_logger import routing_logger

CONSOLIDATION_INTERVAL = int(os.getenv("CONSOLIDATION_INTERVAL", "10"))

# 从统一配置读取，兼容旧版 DEEPSEEK_API_KEY
_API_KEY = os.getenv("CONSOLIDATION_LLM_API_KEY") or os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
_API_URL = os.getenv("CONSOLIDATION_LLM_API_URL") or os.getenv("LLM_API_URL") or "https://api.deepseek.com/v1"
_MODEL_ID = os.getenv("CONSOLIDATION_LLM_MODEL_ID") or os.getenv("LLM_MODEL_ID") or "deepseek-chat"
# 整理用较低 temperature，保证输出稳定
_TEMPERATURE = float(os.getenv("CONSOLIDATION_TEMPERATURE", "0.0"))
_MAX_TOKENS = int(os.getenv("CONSOLIDATION_MAX_TOKENS", "4096"))

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "consolidation_prompt.txt"
_PLAYER_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "player_profile_consolidation_prompt.txt"


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


def _load_consolidation_prompt() -> str:
    """从外部文件加载整合 prompt 模板"""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _load_player_profile_prompt() -> str:
    """从外部文件加载玩家档案整理 prompt 模板"""
    return _PLAYER_PROMPT_PATH.read_text(encoding="utf-8")

# 日期标题正则：只匹配「整行就是日期标题」的情况，避免误伤正文
# 匹配：## 4月5日 / **4月5日** / 4月5日（行首且行尾，不含其他文字）
_DATE_HEADING_RE = re.compile(
    r"^(?:##\s*|\*\*)?(\d{1,2}月\d{1,2}日)(?:\*\*)?\s*$",
)


def normalize(content: str) -> str:
    """修复常见格式问题：字面\\n、日期标题不规范"""
    # 1. 字面 \n → 真换行
    content = content.replace("\\n", "\n")
    # 2. 清理 HTML 注释
    content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
    # 3. 统一日期标题为 ## X月X日（只处理独占一行的日期标题）
    lines = content.split("\n")
    out = []
    for line in lines:
        m = _DATE_HEADING_RE.match(line.strip())
        if m:
            out.append(f"## {m.group(1)}")
        else:
            out.append(line)
    # 4. 压缩连续空行
    content = "\n".join(out)
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content


def split_by_date(content: str) -> OrderedDict[str, str]:
    """按 ## X月X日 切分，同日期自动合并。

    Returns:
        sections: 日期 → 合并后的内容（保持出现顺序）
    """
    sections: OrderedDict[str, str] = OrderedDict()
    current_date = None
    current_lines: list[str] = []

    for line in content.split("\n"):
        m = re.match(r"^##\s*(\d{1,2}月\d{1,2}日)$", line.strip())
        if m:
            if current_date:
                body = "\n".join(current_lines).strip()
                if current_date in sections:
                    sections[current_date] += "\n" + body
                else:
                    sections[current_date] = body
            current_date = m.group(1)
            current_lines = []
        elif current_date:
            current_lines.append(line)
        # 忽略日期之前的内容（标题行等）

    # 最后一段
    if current_date:
        body = "\n".join(current_lines).strip()
        if current_date in sections:
            sections[current_date] += "\n" + body
        else:
            sections[current_date] = body

    return sections


class MemoryConsolidator:
    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        self._client: httpx.AsyncClient | None = None

    def _get_lock(self, name: str) -> asyncio.Lock:
        if name not in self._locks:
            self._locks[name] = asyncio.Lock()
        return self._locks[name]

    async def _call_llm(self, prompt: str) -> str:
        """调用 LLM 进行记忆整理，使用统一配置"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=120.0)

        if not _API_KEY:
            raise ValueError(
                "LLM API key not configured. "
                "Please set LLM_API_KEY or CONSOLIDATION_LLM_API_KEY"
            )

        # 构建 chat completions 端点 URL
        api_url = _API_URL.rstrip("/")
        if not api_url.endswith("/chat/completions"):
            api_url = f"{api_url}/chat/completions"

        resp = await self._client.post(
            api_url,
            headers={
                "Authorization": f"Bearer {_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": _MODEL_ID,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": _TEMPERATURE,
                "max_tokens": _MAX_TOKENS,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()

    def _state_path(self, agent_name: str) -> Path:
        """整合进度文件路径"""
        return Path(f"agents/{agent_name}/memory/.consolidation_state.json")

    def _load_state(self, agent_name: str) -> str | None:
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

    async def consolidate_agent(
        self, agent_name: str
    ) -> _ConsolidationResult | None:
        """整理单个 agent 的记忆，返回结果摘要（供 consolidate_all 汇总日志）"""
        result = _ConsolidationResult(agent_name=agent_name)
        lock = self._get_lock(agent_name)
        if lock.locked():
            result.skipped = True
            result.skip_reason = "已有整理任务在运行"
            return result

        async with lock:
            path = Path(f"agents/{agent_name}/memory/Memory.md")
            if not path.exists():
                return None
            original_content = path.read_text(encoding="utf-8")
            if len(original_content.strip()) < 500:
                return None

            # normalize + split（同日期段落自动合并）
            content = normalize(original_content)
            sections = split_by_date(content)
            if len(sections) < 2:
                return None

            # 根据进度确定需要整合的日期范围
            all_dates = list(sections.keys())
            last_consolidated = self._load_state(agent_name)

            if last_consolidated:
                if last_consolidated in all_dates:
                    start_idx = all_dates.index(last_consolidated)
                else:
                    # 进度日期已不存在（文件被手动编辑？），跳过本次整合
                    routing_logger.warning(
                        f"[整理器] {agent_name} 进度日期 '{last_consolidated}' "
                        f"在文件中不存在，跳过本次整合"
                    )
                    return None
            else:
                # 无进度记录，从头开始
                start_idx = 0

            dates_to_consolidate = all_dates[start_idx:]

            if not dates_to_consolidate:
                return None

            result.days = len(dates_to_consolidate)
            result.date_range = (
                f"{dates_to_consolidate[0]}~{dates_to_consolidate[-1]}"
            )
            result.original_len = len(original_content)

            # 备份
            bak_dir = Path(f"agents/{agent_name}/memory/bak")
            bak_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            bak_path = bak_dir / f"Memory_{ts}_pre.md"
            shutil.copy2(path, bak_path)

            # 只保留最近15个备份
            bak_files = sorted(
                bak_dir.glob("Memory_*_pre.md"),
                key=lambda f: f.stat().st_mtime,
            )
            if len(bak_files) > 15:
                for old_bak in bak_files[:-15]:
                    old_bak.unlink()

            # 对需要整合的日期逐个调 LLM
            for date in dates_to_consolidate:
                full_text = f"## {date}\n{sections[date]}"
                prompt = _load_consolidation_prompt().format(content=full_text)
                try:
                    llm_result = await self._call_llm(prompt)
                    if len(llm_result.strip()) < 20:
                        result.errors.append(f"{date} LLM返回过短")
                        continue
                    # 去掉 LLM 可能重复输出的日期标题
                    llm_result = re.sub(
                        r"^##\s*\d{1,2}月\d{1,2}日\s*\n?", "", llm_result
                    ).strip()
                    sections[date] = llm_result
                except Exception as e:
                    result.errors.append(f"{date}: {e}")
                    routing_logger.error(
                        f"[整理器] {agent_name} {date} 整合失败: {e}"
                    )

            result.final_len = self._write_back(
                path, agent_name, sections, original_content
            )

            # 更新进度：记录到倒数第二个日期（最新一天可能还在产生新记忆）
            if len(all_dates) >= 2:
                self._save_state(agent_name, all_dates[-2])
            # 只有1天时不记录进度，下次仍会整合

            # 顺带整理 user.md
            user_before, user_after = await self._consolidate_player_profile(
                agent_name
            )
            result.user_md_before = user_before
            result.user_md_after = user_after

            return result

    def _write_back(self, path: Path, agent_name: str,
                    sections: OrderedDict[str, str],
                    original_content: str) -> int:
        """写回整理后的内容，同时保护并发追加的新记忆。返回最终字符数。"""
        # 检查文件是否在整理期间被追加了新内容
        current_content = path.read_text(encoding="utf-8")
        if len(current_content) > len(original_content):
            # 有新内容被追加，提取增量部分
            appended = current_content[len(original_content):]
            routing_logger.info(
                f"[整理器] {agent_name} 检测到并发追加 ({len(appended)} 字符)，将保留"
            )
        else:
            appended = ""

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

    async def _consolidate_player_profile(
        self, agent_name: str
    ) -> tuple[int, int]:
        """整理单个角色的 user.md（去重精炼）。返回 (原始长度, 整理后长度)。"""
        user_path = Path(f"agents/{agent_name}/user.md")
        if not user_path.exists():
            return 0, 0

        content = user_path.read_text(encoding="utf-8")

        # 内容太短不需要整理
        if len(content.strip()) < 100:
            return 0, 0

        try:
            # 备份
            bak_dir = Path(f"agents/{agent_name}/memory/bak")
            bak_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            bak_path = bak_dir / f"user_{ts}_pre.md"
            shutil.copy2(user_path, bak_path)

            # 只保留最近 5 个 user.md 备份
            user_baks = sorted(
                bak_dir.glob("user_*_pre.md"),
                key=lambda f: f.stat().st_mtime,
            )
            if len(user_baks) > 5:
                for old_bak in user_baks[:-5]:
                    old_bak.unlink()

            prompt = _load_player_profile_prompt().format(content=content)
            consolidated = await self._call_llm(prompt)

            if len(consolidated.strip()) < 20:
                routing_logger.warning(
                    f"[整理器] {agent_name} user.md LLM 返回过短，跳过"
                )
                return 0, 0

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
                f"[整理器] {agent_name} user.md 整理完成 "
                f"(长度: {before} → {after})"
            )

    async def consolidate_all(self, agent_names: list[str]):
        t0 = time.monotonic()

        # 收集各 agent 的摘要信息用于开始日志
        summaries: list[str] = []
        for name in agent_names:
            path = Path(f"agents/{name}/memory/Memory.md")
            if path.exists():
                length = len(path.read_text(encoding="utf-8"))
                summaries.append(f"{name}({length}字)")
            else:
                summaries.append(f"{name}(无文件)")

        routing_logger.info(
            f"[整理器] 开始记忆整理: {', '.join(summaries)}"
        )

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
                routing_logger.info(
                    f"[整理器] {r.agent_name} 跳过: {r.skip_reason}"
                )
                continue

            # 构建压缩率
            if r.original_len > 0:
                ratio = (1 - r.final_len / r.original_len) * 100
                mem_part = (
                    f"{r.original_len}→{r.final_len}字"
                    f"({ratio:+.1f}%)"
                )
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
        if self._client and not self._client.is_closed:
            await self._client.aclose()


memory_consolidator = MemoryConsolidator()
