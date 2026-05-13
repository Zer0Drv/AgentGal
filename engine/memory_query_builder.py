"""Build character retrieval queries from visible scene context."""

import re
from dataclasses import dataclass
from typing import Any

from memory.parser import extract_status_field
from shared.narrator_output import extract_narrator_output, raw_message_text
from shared.text_utils import role_to_speaker
from storage.agent_files import read_agent_file


_RECENT_LIMIT = 4          # last N visible messages to include as context
_QUERY_TEXT_LIMIT = 1800   # semantic query max chars (embedding model context)
_BM25_TEXT_LIMIT = 700     # lexical query max chars (BM25 search)
_UNDERSTANDING_TEXT_LIMIT = 1200


@dataclass(frozen=True)
class RetrievalQueries:
    episode: str
    episode_bm25: str
    understanding: str
    understanding_bm25: str


def get_narrative_focus(agent_name: str) -> str:
    """Read narrator status.md narrative focus; returns empty string on failure."""
    if agent_name == "narrator":
        return ""
    status = read_agent_file("narrator", "status.md")
    return extract_status_field(status, "叙事焦点").strip()


def _clip(text: str, limit: int = 280, *, normalize: bool = True) -> str:
    if normalize:
        text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _scene_text(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    parts = [
        " ".join(str(payload.get(key) or "").strip() for key in ("date", "time")).strip(),
        str(payload.get("location") or "").strip(),
    ]
    if scene := str(payload.get("scene_description") or "").strip():
        parts.append(_clip(scene, 180))
    return "\n".join(part for part in parts if part)


def _message_line(msg: dict, text: str) -> str:
    return f"{role_to_speaker(str(msg.get('role', 'unknown')))}: {_clip(text)}" if text else ""


def build_retrieval_queries(
    agent_name: str,
    user_input: str,
    raw_messages: list[dict] | None = None,
    *,
    focus: str | None = None,
) -> RetrievalQueries:
    """Build all retrieval queries from the same visible scene context."""
    visible = [
        msg
        for msg in raw_messages or []
        if agent_name == "narrator" or agent_name in msg.get("visible_to", [])
    ][-_RECENT_LIMIT:]

    # Pre-compute payloads once to avoid duplicate extract_narrator_output calls
    msg_payloads: list[tuple[dict, dict[str, Any] | None]] = [
        (msg, extract_narrator_output(msg)) for msg in visible
    ]

    # Find the most recent narrator scene message
    scene_payload: dict[str, Any] | None = None
    for _msg, payload in reversed(msg_payloads):
        if payload:
            scene_payload = payload
            break

    dialogue_lines: list[str] = []
    bm25_parts: list[str] = []
    for msg, payload in msg_payloads:
        if scene_payload is not None and payload and payload is scene_payload:
            continue
        if payload:
            text = _scene_text(payload)
        else:
            text = raw_message_text(msg)
            if text:
                bm25_parts.append(text)
        if text:
            dialogue_lines.append(_message_line(msg, text))

    scene_text = _scene_text(scene_payload)
    dialogue = "\n".join(dialogue_lines)
    bm25_text = "\n".join(bm25_parts)
    if focus is None:
        focus = get_narrative_focus(agent_name)
    episode_query = "\n\n".join(
        part
        for part in [
            f"当前场景：\n{scene_text}" if scene_text else "",
            f"最近对话：\n{dialogue}" if dialogue else "",
            f"叙事焦点：{focus}" if focus else "",
        ]
        if part
    )
    episode_bm25 = "\n".join(part for part in [focus, scene_text, bm25_text] if part)
    understanding_query = "\n\n".join(
        part
        for part in [
            f"关系/行为焦点：{focus}" if focus else "",
            f"近期可见对话：\n{dialogue}" if dialogue else "",
        ]
        if part
    )
    understanding_bm25 = "\n".join(part for part in [focus, bm25_text] if part)
    return RetrievalQueries(
        episode=_clip(episode_query, _QUERY_TEXT_LIMIT) if episode_query else user_input,
        episode_bm25=_clip(episode_bm25, _BM25_TEXT_LIMIT, normalize=False)
        if episode_bm25
        else user_input,
        understanding=_clip(understanding_query, _UNDERSTANDING_TEXT_LIMIT)
        if understanding_query
        else user_input,
        understanding_bm25=_clip(understanding_bm25, _BM25_TEXT_LIMIT, normalize=False)
        if understanding_bm25
        else user_input,
    )
