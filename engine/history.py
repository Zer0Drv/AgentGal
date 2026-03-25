"""对话历史处理 - 可见性过滤与高低水位窗口，将原始消息转为 transcript"""

import re

from shared.config import HISTORY_HIGH, HISTORY_LOW
from storage.agent_files import read_sidecar_json, write_sidecar_json
from storage.history import load_conversation_history  # noqa: F401 — re-export for engine callers
from log_config.routing import routing_logger


def _apply_high_low_watermark(
    agent_name: str,
    visible_indices: list[int],
    start_raw_index: int,
    high: int,
    low: int,
) -> tuple[int, list[int]]:
    """应用高低水位缓冲，返回本轮应显示的消息下标范围。

    Args:
        agent_name: 角色名，仅用于日志。
        visible_indices: 该 agent 可见的消息在 raw 列表中的绝对下标，已按时间升序排列。
        start_raw_index: 上轮持久化的窗口左边界（raw 绝对下标），用于跳过已截断的旧消息。
        high: 窗口上限；当前窗口消息数超过此值时触发截断。
        low: 截断目标；触发时保留最新的 low 条消息。

    Returns:
        (next_start_raw_index, kept_indices)：
        - next_start_raw_index：新的窗口左边界，调用方应持久化以供下轮使用。
        - kept_indices：本轮实际送入 context 的消息下标列表（raw 绝对下标子集）。
    """
    if not visible_indices:
        return 0, []

    # 用持久化的 start_raw_index 作为窗口左边界，过滤掉已被截断的旧消息
    kept_indices = [idx for idx in visible_indices if idx >= start_raw_index]
    if not kept_indices:
        # 窗口起点比所有可见消息都新（通常发生在重置/加载后），回退到全部可见
        start_raw_index = visible_indices[0]
        kept_indices = visible_indices

    if len(kept_indices) > high:
        # 触发高水位：一次性砍到 low 条，并把新的窗口起点持久化回去
        # 下次调用前不会再截断，只在尾部追加，直到再次超过 high
        routing_logger.info("[%s] 历史窗口触发高水位", agent_name)
        kept_indices = kept_indices[-low:]
        start_raw_index = kept_indices[0]

    return start_raw_index, kept_indices


def _get_windowed_visible_messages(agent_name: str, raw_messages: list[dict]) -> list[dict]:
    """按 agent 维度应用真正的高低水位窗口，并持久化窗口起点。"""
    if not raw_messages:
        return []

    # narrator 看全部消息；角色只看 visible_to 中包含自己的消息
    if agent_name != "narrator":
        visible_indices = [
            idx for idx, msg in enumerate(raw_messages) if agent_name in msg.get("visible_to", [])
        ]
    else:
        visible_indices = list(range(len(raw_messages)))

    if not visible_indices:
        return []

    # 从 sidecar 读取上次截断后的窗口起点（raw 消息列表中的绝对下标）
    _hw = read_sidecar_json(agent_name, ".history_window_state.json")
    start_raw_index = min(max(0, int(_hw.get("start_raw_index", 0))), len(raw_messages) - 1)
    next_start_raw_index, kept_indices = _apply_high_low_watermark(
        agent_name,
        visible_indices,
        start_raw_index,
        HISTORY_HIGH,
        HISTORY_LOW,
    )
    # 持久化新的窗口起点，供下次调用时直接跳过已截断的旧消息
    write_sidecar_json(agent_name, ".history_window_state.json", {"start_raw_index": max(0, next_start_raw_index)})
    return [raw_messages[idx] for idx in kept_indices]


def build_history_transcript(
    agent_name: str,
    raw_messages: list[dict],
) -> str:
    """将 JSONL 原始消息转为单段历史文本。

    规则：
    - visible_to 过滤（narrator 看全部，角色只看自己可见的）
    - 真正的高低水位窗口：超过 HISTORY_HIGH 时砍到 HISTORY_LOW，之后只追加直到再次超限
    - 保留原始消息边界，不拆成多条 user/assistant message
    - 统一格式化为带说话者前缀的单段文本，利于放进一条大 user message
    """
    visible = _get_windowed_visible_messages(agent_name, raw_messages)

    if not visible:
        return ""

    lines: list[str] = []
    for msg in visible:
        role = msg.get("role", "unknown")
        content = re.sub(r"\n+", "\n", msg.get("content", "").strip())
        if not content:
            continue
        speaker = "玩家" if role == "player" else role
        lines.append(f"{speaker}: {content}")

    return "\n\n".join(lines)
