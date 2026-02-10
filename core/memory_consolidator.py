"""后台记忆整理器 - 远期记忆衰减压缩

根据日期距离当前游戏日期，分三档压缩：
- raw（最近 2 天）：保持原文不动
- condensed（3-5 天前）：LLM 压缩为关键事件摘要（~10 行/天）
- summary（5 天以上）：LLM 压缩为一句话摘要（2-3 行/天）


"""

import os
import re
import json
import shutil
import asyncio
from datetime import datetime
from pathlib import Path
import httpx
from .routing_logger import routing_logger

# 整理间隔（每多少轮对话触发一次）
CONSOLIDATION_INTERVAL = int(os.getenv("CONSOLIDATION_INTERVAL", "10"))

# 衰减阈值（以游戏日期天数计）
RAW_DAYS = int(os.getenv("RAW_DAYS", "2"))          # 最近 N 天保持原文
CONDENSED_DAYS = int(os.getenv("CONDENSED_DAYS", "5"))  # N 天内压缩为关键事件

# 状态文件
STATE_FILENAME = ".consolidation_state"

# DeepSeek API
API_URL = "https://api.deepseek.com/chat/completions"

# ── Prompt 模板 ──────────────────────────────────────────

CONDENSE_PROMPT = """\
你是一个记忆压缩助手。请将以下角色的某一天详细记忆压缩为**关键事件摘要**。

要求：
1. 保留所有重要事件、关系变化、情感转折点
2. 删除重复描述、过程细节、内心独白的冗余部分
3. 每个场景压缩为 1-3 条关键信息
4. 保持 `**时间段 · 地点**` + `- 内容` 的格式
5. 保留日期标题 `## X月X日`
6. 用角色自己的口吻和视角书写，保留情感色彩，像是角色在回忆这件事，而不是第三方在做剧情总结
7. 只输出压缩后的内容，不要任何说明

原始记忆：

{content}
"""

SUMMARIZE_PROMPT = """\
你是一个记忆压缩助手。请将以下角色的某一天记忆压缩为**极简摘要**（2-3 行）。

要求：
1. 用 2-3 个 `- ` 条目概括这一天最重要的事件和关系变化
2. 只保留对后续剧情有影响的关键信息（告白、冲突、秘密暴露、关系转折）
3. 保留日期标题 `## X月X日`
4. 用角色自己的口吻和视角书写，保留情感色彩，像是角色在回忆这件事，而不是第三方在做剧情总结
5. 只输出摘要，不要任何说明

原始记忆：

{content}
"""




class MemoryConsolidator:
    """
    远期记忆衰减整理器

    压缩级别：raw → condensed → summary
    每次整理时根据日期距离重新评估，逐步压缩旧记忆。
    """

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        self._client: httpx.AsyncClient | None = None

    def _get_lock(self, agent_name: str) -> asyncio.Lock:
        if agent_name not in self._locks:
            self._locks[agent_name] = asyncio.Lock()
        return self._locks[agent_name]

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=120.0)
        return self._client

    async def _call_llm(self, prompt: str) -> str:
        """调用 DeepSeek API"""
        api_key = os.getenv("DEEPSEEK_API_KEY")
        model_id = os.getenv("MODEL_ID", "deepseek-chat")
        client = await self._get_client()
        response = await client.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 4096,
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"].strip()

    # ── 文件路径 ──

    def _get_memory_path(self, agent_name: str) -> Path:
        return Path(f"agents/{agent_name}/memory/Memory.md")

    def _get_state_path(self, agent_name: str) -> Path:
        return Path(f"agents/{agent_name}/memory/{STATE_FILENAME}")

    # ── 状态管理 ──

    def _load_state(self, agent_name: str) -> dict:
        state_path = self._get_state_path(agent_name)
        if state_path.exists():
            try:
                return json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        # date_levels: {"4月5日": "raw", "4月6日": "condensed", ...}
        return {"date_levels": {}, "last_run": None}

    def _save_state(self, agent_name: str, state: dict):
        state_path = self._get_state_path(agent_name)
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ── 解析工具 ──

    def _parse_dates(self, content: str) -> list[str]:
        """提取所有日期标题（按顺序）"""
        pattern = r"^##\s*(\d{1,2}月\d{1,2}日.*)$"
        dates = []
        for line in content.split("\n"):
            match = re.match(pattern, line.strip())
            if match:
                dates.append(match.group(1).strip())
        return dates

    def _extract_date_content(self, content: str, target_date: str) -> str:
        """提取指定日期的完整内容（含标题行）"""
        lines = content.split("\n")
        result = []
        in_target = False

        for line in lines:
            date_match = re.match(
                r"^##\s*(\d{1,2}月\d{1,2}日.*)$", line.strip()
            )
            if date_match:
                current_date = date_match.group(1).strip()
                if in_target:
                    break
                if current_date == target_date:
                    in_target = True
                    result.append(line)
                continue
            if in_target:
                result.append(line)

        return "\n".join(result)

    def _extract_header(self, content: str) -> str:
        """提取文件标题行（# xxx 的长期记忆）"""
        for line in content.split("\n"):
            if line.strip().startswith("# "):
                return line.strip()
        return ""

    # ── 压缩级别判定 ──

    def _determine_target_level(
        self, date_index: int, total_dates: int
    ) -> str:
        """根据日期距离最新日期，决定目标压缩级别"""
        distance = total_dates - 1 - date_index  # 距离最新日期的天数

        if distance < RAW_DAYS:
            return "raw"
        elif distance < CONDENSED_DAYS:
            return "condensed"
        else:
            return "summary"

    # ── 核心整理逻辑 ──

    async def consolidate_agent(self, agent_name: str):
        """对单个角色执行远期记忆衰减压缩"""
        lock = self._get_lock(agent_name)

        if lock.locked():
            routing_logger.info(
                f"[整理器] {agent_name} 已有整理任务在运行，跳过"
            )
            return

        async with lock:
            memory_path = self._get_memory_path(agent_name)
            if not memory_path.exists():
                return

            content = memory_path.read_text(encoding="utf-8")
            if len(content.strip()) < 500:
                return

            all_dates = self._parse_dates(content)
            if len(all_dates) < 2:
                return

            # 加载状态
            state = self._load_state(agent_name)
            date_levels = state.get("date_levels", {})

            # 判断哪些日期需要压缩
            tasks_to_do: list[tuple[str, str, str]] = []
            # (date, current_level, target_level)

            for i, date in enumerate(all_dates):
                target = self._determine_target_level(i, len(all_dates))
                current = date_levels.get(date, "raw")

                # 只做「更深」的压缩，不回退
                level_order = ["raw", "condensed", "summary"]
                if level_order.index(target) > level_order.index(current):
                    tasks_to_do.append((date, current, target))

            if not tasks_to_do:
                routing_logger.info(
                    f"[整理器] {agent_name} 无需压缩 "
                    f"(共 {len(all_dates)} 天记忆)"
                )
                return

            routing_logger.info(
                f"[整理器] {agent_name} 开始衰减压缩: "
                + ", ".join(
                    f"{d} ({c}→{t})" for d, c, t in tasks_to_do
                )
            )

            # 备份
            bak_dir = Path(f"agents/{agent_name}/memory/bak")
            bak_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            bak_path = bak_dir / f"Memory_{timestamp}_pre_consolidation.md"
            try:
                shutil.copy2(memory_path, bak_path)
            except Exception as e:
                routing_logger.error(
                    f"[整理器] {agent_name} 备份失败: {e}，跳过整理"
                )
                return

            try:
                # 提取每个日期的内容
                date_contents: dict[str, str] = {}
                for date in all_dates:
                    date_contents[date] = self._extract_date_content(
                        content, date
                    )

                # 逐个压缩需要处理的日期
                for date, _current_level, target_level in tasks_to_do:
                    original_text = date_contents[date]
                    original_lines = original_text.count("\n")

                    if target_level == "condensed":
                        prompt = CONDENSE_PROMPT.format(content=original_text)
                    else:  # summary
                        prompt = SUMMARIZE_PROMPT.format(content=original_text)

                    compressed = await self._call_llm(prompt)

                    # 安全检查：压缩结果不能为空
                    if len(compressed.strip()) < 20:
                        routing_logger.warning(
                            f"[整理器] {agent_name} {date} "
                            f"压缩结果过短，跳过"
                        )
                        continue

                    date_contents[date] = compressed
                    date_levels[date] = target_level
                    new_lines = compressed.count("\n")
                    routing_logger.info(
                        f"[整理器] {agent_name} {date}: "
                        f"{original_lines}行 → {new_lines}行 "
                        f"({target_level})"
                    )

                # 重新组装文件
                header = self._extract_header(content)
                if not header:
                    header = f"# {agent_name} 的长期记忆"

                parts = [header, ""]

                # 按日期顺序拼接
                for date in all_dates:
                    parts.append(date_contents[date].strip())
                    parts.append("")

                new_content = "\n".join(parts).strip() + "\n"

                # 写入
                memory_path.write_text(new_content, encoding="utf-8")

                # 更新状态
                state["date_levels"] = date_levels
                state["last_run"] = datetime.now().isoformat()
                self._save_state(agent_name, state)

                old_lines = content.count("\n")
                new_lines = new_content.count("\n")
                routing_logger.info(
                    f"[整理器] {agent_name} 整理完成: "
                    f"{old_lines}行 → {new_lines}行, "
                    f"压缩了 {len(tasks_to_do)} 个日期"
                )

            except Exception as e:
                routing_logger.error(
                    f"[整理器] {agent_name} 整理失败: {e}"
                )
                if bak_path.exists():
                    shutil.copy2(bak_path, memory_path)
                    routing_logger.info(
                        f"[整理器] {agent_name} 已从备份恢复"
                    )

    async def consolidate_all(self, agent_names: list[str]):
        """并行整理所有角色的记忆"""
        routing_logger.info(f"[整理器] 开始后台记忆整理: {agent_names}")
        tasks = [self.consolidate_agent(name) for name in agent_names]
        await asyncio.gather(*tasks, return_exceptions=True)
        routing_logger.info("[整理器] 后台记忆整理完成")

    async def close(self):
        """关闭 HTTP 客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# 全局实例
memory_consolidator = MemoryConsolidator()
