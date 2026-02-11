"""后台记忆整理器 - 去重 + 压缩

normalize → split_by_date（天然去重）→ condense → write

所有日期统一用 condense 级别：去重 + 压缩为关键事件摘要。
行数 ≤ 10 的日期视为已整理，跳过。
"""

import os
import re
import shutil
import asyncio
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
import httpx
from .routing_logger import routing_logger

CONSOLIDATION_INTERVAL = int(os.getenv("CONSOLIDATION_INTERVAL", "10"))
CONDENSE_THRESHOLD = int(os.getenv("CONDENSE_THRESHOLD", "6"))

API_URL = "https://api.deepseek.com/chat/completions"

CONDENSE_PROMPT = """\
<task>将以下角色的某一天详细记忆压缩为关键事件摘要，尽量不超过 5 条。</task>

<merge_rules>
- 同一时间段 + 同一地点的多条记忆合并为一条
- 相邻时间段的连续行动也可合并
- 重复出现的同类情绪只保留一次
</merge_rules>

<priority>
1. 关系转折点（告白、决裂、和解等）
2. 关键数值变化（如契约进度）
3. 具体行为和对话（谁做了什么、说了什么）
4. 情绪反应（仅保留新出现的情绪）
</priority>

<format>
- 保持 ## X月X日 标题
- 每条格式：- **时间段/地点**：事件描述
- 地点保留专有名词，不要泛化
- 用角色自己的口吻书写
- 只输出压缩结果，不要任何说明
</format>

<input>
{content}
</input>
"""

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
    """按 ## X月X日 切分，同日期自动合并（天然去重）"""
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
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=120.0)
        api_key = os.getenv("DEEPSEEK_API_KEY")
        resp = await self._client.post(
            API_URL,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"model": os.getenv("MODEL_ID", "deepseek-chat"),
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.1, "max_tokens": 4096},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()

    def _needs_condense(self, body: str) -> bool:
        """行数超过阈值则需要整理"""
        lines = [line for line in body.split("\n") if line.strip()]
        return len(lines) > CONDENSE_THRESHOLD

    async def consolidate_agent(self, agent_name: str):
        lock = self._get_lock(agent_name)
        if lock.locked():
            return

        async with lock:
            path = Path(f"agents/{agent_name}/memory/Memory.md")
            if not path.exists():
                return
            original_content = path.read_text(encoding="utf-8")
            if len(original_content.strip()) < 500:
                return

            # normalize + split
            content = normalize(original_content)
            sections = split_by_date(content)
            if len(sections) < 2:
                return

            # 找出需要压缩的日期（行数 > 阈值）
            to_condense = [
                date for date, body in sections.items()
                if self._needs_condense(body)
            ]

            if not to_condense:
                routing_logger.info(
                    f"[整理器] {agent_name} 无需压缩 ({len(sections)} 天)"
                )
                # 即使不压缩，也写回 normalize 后的内容（修复格式）
                self._write_back(path, agent_name, sections, original_content)
                return

            routing_logger.info(
                f"[整理器] {agent_name} 压缩: {', '.join(to_condense)}"
            )

            # 备份
            bak_dir = Path(f"agents/{agent_name}/memory/bak")
            bak_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            shutil.copy2(path, bak_dir / f"Memory_{ts}_pre.md")

            # 逐个压缩
            for date in to_condense:
                full_text = f"## {date}\n{sections[date]}"
                prompt = CONDENSE_PROMPT.format(content=full_text)
                try:
                    result = await self._call_llm(prompt)
                    if len(result.strip()) < 20:
                        continue
                    # 去掉 LLM 可能重复输出的日期标题
                    result = re.sub(
                        r"^##\s*\d{1,2}月\d{1,2}日\s*\n?", "", result
                    ).strip()
                    sections[date] = result
                    routing_logger.info(f"[整理器] {agent_name} {date} 已压缩")
                except Exception as e:
                    routing_logger.error(
                        f"[整理器] {agent_name} {date} 失败: {e}"
                    )

            self._write_back(path, agent_name, sections, original_content)
            routing_logger.info(f"[整理器] {agent_name} 整理完成")

    def _write_back(self, path: Path, agent_name: str,
                    sections: OrderedDict[str, str],
                    original_content: str):
        """写回整理后的内容，同时保护并发追加的新记忆"""
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

    async def consolidate_all(self, agent_names: list[str]):
        routing_logger.info(f"[整理器] 开始后台记忆整理: {agent_names}")
        await asyncio.gather(
            *(self.consolidate_agent(n) for n in agent_names),
            return_exceptions=True,
        )
        routing_logger.info("[整理器] 后台记忆整理完成")

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


memory_consolidator = MemoryConsolidator()
