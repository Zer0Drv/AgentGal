"""角色文件 ↔ 实体仓库：soul 加载（带缓存）+ status / memory 写回策略。

吸收原 Character/BaseEntity 的持久化职责（set_status_fields / append_memory /
add_event / mark_triggered 的 value-add 逻辑）。不含编排（搜记忆 / 跑 SDK 在 Service）。
"""

from models import Character, status_fields
from log_config.routing import routing_logger
from shared.text_utils import normalize
from storage.agent_files import read_agent_file
from storage.memory_store import append_memory_draft
from storage.runtime_state import read_turn_counter
from storage.status_file import (
    FileUpdateResult,
    add_pending_event,
    mark_event_triggered,
    update_status,
)


class CharacterRepository:
    """游戏角色的文件读写仓库；soul 缓存随存档生命周期，load/reset 后失效。"""

    def __init__(self) -> None:
        self._soul_cache: dict[str, str] = {}

    # ── 读 ──

    def load(self, name: str) -> Character:
        """读 soul.md（带缓存）并返回 Character 实体。"""
        soul = self._soul_cache.get(name)
        if soul is None:
            soul = read_agent_file(name, "soul.md")
            self._soul_cache[name] = soul
        return Character(name=name, soul=soul)

    def read_status_text(self, name: str) -> str:
        """实时读取 status.md 原文（运行时动态文件，不缓存）。"""
        return read_agent_file(name, "status.md")

    def invalidate(self, name: str | None = None) -> None:
        """存档恢复 / reset 后清空 soul 缓存（name=None 清全部）。"""
        if name is None:
            self._soul_cache.clear()
        else:
            self._soul_cache.pop(name, None)

    # ── 写回策略 ──

    def apply_status_fields(self, name: str, fields: dict[str, str]) -> list[FileUpdateResult]:
        """按字段合并更新 status.md；事件段禁止整段覆写、空值跳过。"""
        results: list[FileUpdateResult] = []
        for field, content in fields.items():
            if not content:
                continue
            if field in status_fields.EVENT_SECTIONS:
                routing_logger.warning(
                    "[%s] apply_status_fields 拒绝写入事件段 %r；请用 add_event / mark_triggered",
                    name,
                    field,
                )
                continue
            try:
                results.append(update_status(name, field, str(content)))
            except Exception as e:
                routing_logger.error(f"[{name}] status[{field}] 失败: {e}")
        return results

    def append_memory(self, name: str, text: str) -> FileUpdateResult | None:
        """把本轮 memory 片段 normalize + 标当前 narrator turn 后追加到 memory_draft.jsonl。"""
        if not text:
            return None
        normalized = normalize(text)
        if not normalized:
            return None
        try:
            turn = read_turn_counter()
            append_memory_draft(name, turn, normalized)
            return FileUpdateResult(
                file="memory_draft.jsonl",
                target="长期记忆",
                operation="append",
                appended=normalized,
            )
        except Exception as e:
            routing_logger.error(f"[{name}] memory_draft 写入失败: {e}")
            return None

    def add_event(self, name: str, desc: str) -> FileUpdateResult | None:
        """向「打算」事件段追加一条；desc 为 '无' 时跳过。"""
        if desc.strip() == "无":
            return None
        try:
            return add_pending_event(name, desc, status_fields.PLANS)
        except Exception as e:
            routing_logger.error(f"[{name}] add_event 失败: {e}")
            return None

    def mark_triggered(self, name: str, event_name: str) -> FileUpdateResult | None:
        """从「打算」事件段移除一条已触发事件。"""
        try:
            return mark_event_triggered(name, event_name, status_fields.PLANS)
        except Exception as e:
            routing_logger.error(f"[{name}] mark_triggered 失败: {e}")
            return None


character_repo = CharacterRepository()
