"""旁白文件 ↔ 状态仓库：soul / status / world_schedule 的读写。

narrator 无 memory / draft，字段与角色不同（场景 / 角色位置 / 待触发事件 / world_schedule）。
方法只吃原始值，LLM DTO 的拆解留在 NarratorService（反腐层），storage 不依赖 agents。
"""

from models import status_fields
from log_config.routing import routing_logger
from shared.config import get_agent_names
from shared.text_utils import extract_status_field, get_display_name
from storage.agent_files import read_agent_file, read_sidecar_json, write_sidecar_json
from storage.status_file import (
    FileUpdateResult,
    add_pending_event,
    mark_event_triggered,
    update_status,
    update_status_allow_new_field,
)

_NAME = "narrator"
WORLD_SCHEDULE_FILENAME = "world_schedule.json"


class NarratorRepository:
    """旁白文件读写仓库；soul 缓存随存档生命周期，reset 后失效。"""

    def __init__(self) -> None:
        self._soul_cache: str | None = None

    # ── 读 ──

    def load_soul(self) -> str:
        if self._soul_cache is None:
            self._soul_cache = read_agent_file(_NAME, "soul.md")
        return self._soul_cache

    def read_status_text(self) -> str:
        return read_agent_file(_NAME, "status.md")

    def read_world_schedule(self) -> dict:
        return read_sidecar_json(_NAME, WORLD_SCHEDULE_FILENAME)

    def read_world_schedule_text(self) -> str:
        return read_agent_file(_NAME, WORLD_SCHEDULE_FILENAME)

    def invalidate(self) -> None:
        self._soul_cache = None

    # ── 写回 ──

    def write_scene(
        self,
        *,
        scene: str,
        current_time: str,
        character_locations: dict[str, str] | None,
    ) -> list[FileUpdateResult]:
        """同步场景 / 当前时间 / 角色位置（narrator 独占维护的派生字段）。"""
        results = [
            update_status_allow_new_field(_NAME, status_fields.SCENE, scene),
            update_status_allow_new_field(_NAME, status_fields.CURRENT_TIME, current_time),
        ]
        if character_locations:
            lines = [f"- {name}：{loc}" for name, loc in character_locations.items()]
            results.append(
                update_status_allow_new_field(
                    _NAME, status_fields.CHARACTER_LOCATIONS, "\n".join(lines)
                )
            )
        return results

    def set_narrative_focus(self, focus: str) -> FileUpdateResult:
        return update_status(_NAME, status_fields.NARRATIVE_FOCUS, focus)

    def set_recent_world_event(self, text: str) -> FileUpdateResult:
        return update_status_allow_new_field(_NAME, status_fields.RECENT_WORLD_EVENT, text)

    def write_world_schedule(self, schedule: dict) -> FileUpdateResult:
        write_sidecar_json(_NAME, WORLD_SCHEDULE_FILENAME, schedule)
        return FileUpdateResult(
            file=WORLD_SCHEDULE_FILENAME,
            target="world_schedule",
            operation="replace",
        )

    def add_event(self, desc: str) -> FileUpdateResult | None:
        """向「待触发事件」段追加一条；desc 为 '无' 时跳过。"""
        if desc.strip() == "无":
            return None
        try:
            return add_pending_event(_NAME, desc, status_fields.PENDING_EVENTS)
        except Exception as e:
            routing_logger.error(f"[{_NAME}] add_event 失败: {e}")
            return None

    def mark_triggered(self, event_name: str) -> FileUpdateResult | None:
        """从「待触发事件」段移除一条已触发事件。"""
        try:
            return mark_event_triggered(_NAME, event_name, status_fields.PENDING_EVENTS)
        except Exception as e:
            routing_logger.error(f"[{_NAME}] mark_triggered 失败: {e}")
            return None

    def sync_player_relations(self) -> FileUpdateResult:
        """把各角色 status.md 的「和玩家的关系」汇总写入 narrator/status.md。"""
        content = self._format_player_relations()
        return update_status_allow_new_field(_NAME, status_fields.PLAYER_RELATION, content)

    @staticmethod
    def _format_player_relations() -> str:
        lines: list[str] = []
        for agent_name in get_agent_names(include_narrator=False):
            status_content = read_agent_file(agent_name, "status.md")
            relation = extract_status_field(status_content, status_fields.PLAYER_RELATION)
            relation = " ".join(relation.split())
            if not relation:
                continue
            soul_content = read_agent_file(agent_name, "soul.md")
            display_name = get_display_name(agent_name, soul_content)
            lines.append(f"- {display_name}：{relation}")
        return "\n".join(lines) if lines else "（暂无）"


narrator_repo = NarratorRepository()
