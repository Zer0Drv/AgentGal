"""后台记忆整理器 - 合并同日期 + 去重

normalize → split_by_date（合并同日期段落）→ 对有合并的日期调 LLM 去重 → write
→ 触发向量库全量重建

触发 LLM 的条件：同一日期在文件中出现了多个段落（被合并了）。
行数多但无重复的日期不会被动。
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
from .vector_store import vector_store

CONSOLIDATION_INTERVAL = int(os.getenv("CONSOLIDATION_INTERVAL", "10"))

API_URL = "https://api.deepseek.com/chat/completions"

DEDUP_PROMPT = """\
<task>整理以下角色某一天的记忆，去除重复条目，保留所有细节。</task>

<rules>
- 如果同一事件被多次描述，合并为一条，保留最完整的版本
- 不要压缩、不要摘要、不要丢失任何独特的细节
- 按时间顺序排列
- 保持角色第一人称视角
</rules>

<format>
- 保持 ## X月X日 标题
- 每条格式：- **时间段/地点**：事件描述
- 只输出整理结果，不要任何说明
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


def split_by_date(content: str) -> tuple[OrderedDict[str, str], set[str]]:
    """按 ## X月X日 切分，同日期自动合并。

    Returns:
        (sections, merged_dates)
        sections: 日期 → 合并后的内容
        merged_dates: 发生了多段合并的日期集合
    """
    sections: OrderedDict[str, str] = OrderedDict()
    merged_dates: set[str] = set()
    current_date = None
    current_lines: list[str] = []

    for line in content.split("\n"):
        m = re.match(r"^##\s*(\d{1,2}月\d{1,2}日)$", line.strip())
        if m:
            if current_date:
                body = "\n".join(current_lines).strip()
                if current_date in sections:
                    sections[current_date] += "\n" + body
                    merged_dates.add(current_date)
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
            merged_dates.add(current_date)
        else:
            sections[current_date] = body

    return sections, merged_dates


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

    async def consolidate_agent(self, agent_name: str):
        lock = self._get_lock(agent_name)
        if lock.locked():
            routing_logger.info(f"[整理器] {agent_name} 已有整理任务在运行，跳过")
            return

        async with lock:
            path = Path(f"agents/{agent_name}/memory/Memory.md")
            if not path.exists():
                return
            original_content = path.read_text(encoding="utf-8")
            if len(original_content.strip()) < 500:
                return

            # normalize + split（同日期段落自动合并）
            content = normalize(original_content)
            sections, merged_dates = split_by_date(content)
            if len(sections) < 2:
                return

            if not merged_dates:
                routing_logger.info(
                    f"[整理器] {agent_name} 无需整理 ({len(sections)} 天，无重复日期)"
                )
                # 即使无合并，也写回 normalize 后的内容（修复格式）
                self._write_back(path, agent_name, sections, original_content)
                return

            routing_logger.info(
                f"[整理器] {agent_name} 去重整理: {', '.join(sorted(merged_dates))} "
                f"(原始长度: {len(original_content)} 字符)"
            )

            # 备份
            bak_dir = Path(f"agents/{agent_name}/memory/bak")
            bak_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            bak_path = bak_dir / f"Memory_{ts}_pre.md"
            shutil.copy2(path, bak_path)
            routing_logger.info(f"[整理器] {agent_name} 已备份到: {bak_path.name}")

            # 只保留最近15个备份
            bak_files = sorted(
                bak_dir.glob("Memory_*_pre.md"),
                key=lambda f: f.stat().st_mtime,
            )
            if len(bak_files) > 15:
                for old_bak in bak_files[:-15]:
                    old_bak.unlink()

            # 对合并了多段的日期调 LLM 去重整理
            for date in merged_dates:
                full_text = f"## {date}\n{sections[date]}"
                prompt = DEDUP_PROMPT.format(content=full_text)
                try:
                    result = await self._call_llm(prompt)
                    if len(result.strip()) < 20:
                        routing_logger.warning(
                            f"[整理器] {agent_name} {date} LLM 返回过短，跳过"
                        )
                        continue
                    # 去掉 LLM 可能重复输出的日期标题
                    result = re.sub(
                        r"^##\s*\d{1,2}月\d{1,2}日\s*\n?", "", result
                    ).strip()
                    sections[date] = result
                    routing_logger.info(
                        f"[整理器] {agent_name} {date} 去重完成"
                    )
                except Exception as e:
                    routing_logger.error(
                        f"[整理器] {agent_name} {date} 去重失败: {e}"
                    )

            self._write_back(path, agent_name, sections, original_content)

            # 整理后触发向量库全量重建（后台执行，不阻塞）
            final_content = path.read_text(encoding="utf-8")
            asyncio.create_task(vector_store.rebuild(agent_name, final_content))
            routing_logger.info(f"[整理器] {agent_name} 整理完成，向量重建已提交后台")

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

        # 记录长度变化
        original_len = len(original_content)
        final_len = len(result)
        routing_logger.info(
            f"[整理器] {agent_name} 写回完成: {original_len} 字符 → {final_len} 字符 "
            f"(压缩 {original_len - final_len} 字符, {(1 - final_len/original_len)*100:.1f}%)"
        )

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
