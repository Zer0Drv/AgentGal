"""FastAPI 入口"""

# load_dotenv 必须在所有内部模块 import 之前执行，
# 否则 llm/embedding.py 等模块级常量会在 .env 加载前就固化为空值
from dotenv import load_dotenv
load_dotenv()

import asyncio
import json
import re
import traceback
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agents.factory import initialize_conversation_agents, reload_conversation_agent
from consolidation.flow import memory_consolidation_flow
from engine.character import narrator, reset_entities
from engine.conversation_flow import (
    bootstrap_new_characters,
    generate_choices,
    run_agent_in_scene,
)
from memory.parser import (
    EpisodeMemory,
    Understanding,
    read_memory_jsonl,
    read_understandings,
)
from storage.save_manager import (
    delete_save_archive,
    export_save_archive_with_detail,
    has_existing_save,
    import_save_archive,
    list_save_archives,
    load_story_file,
    reset_game,
)
from log_config.routing import routing_logger
from log_config.logfire import setup_logfire
from shared.config import CHARACTERS_DIR, get_agent_names
from shared.text_utils import get_display_name
from storage.agent_files import increment_turn_counter, read_agent_file
from storage.history import load_conversation_history
from storage.message_router import message_router

load_dotenv()

app = FastAPI(title="AgentGal")

STATIC_DIR = Path(__file__).parent / "static"
_LAST_CHOICES_FILE = CHARACTERS_DIR / "last_choices.json"
_pending_state_update_task: asyncio.Task[None] | None = None
_RECENT_HISTORY_LIMIT = 12
_MEMORY_GRAPH_LABEL_LIMIT = 42
_MEMORY_GRAPH_DETAIL_LIMIT = 260
_MEMORY_GRAPH_RAW_LIMIT = 12000


# =============================================================================
# 工具函数
# =============================================================================


def _save_last_choices(choices: list[str]) -> None:
    _LAST_CHOICES_FILE.write_text(json.dumps(choices, ensure_ascii=False), encoding="utf-8")


def _load_last_choices() -> list[str]:
    try:
        return json.loads(_LAST_CHOICES_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _clear_last_choices() -> None:
    _LAST_CHOICES_FILE.unlink(missing_ok=True)


def _get_agent_display_name(agent_name: str) -> str:
    if agent_name == "narrator":
        return "旁白"
    if agent_name == "player":
        return "你"

    soul_content = read_agent_file(agent_name, "soul.md")
    return get_display_name(agent_name, soul_content) if soul_content else agent_name


def _clip_text(value: str, limit: int) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def _clip_preserving_lines(value: str, limit: int) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}…"


def _format_raw_dialogue_preview(raw_dialogue: str, limit: int) -> str:
    text = (raw_dialogue or "").strip()
    if not text:
        return ""
    text = text[: limit * 3]

    entries: list[str] = []
    for segment in re.split(r"\s*(?=\[turn=\d+\]\s*)", text):
        cleaned = re.sub(r"^\[turn=\d+\]\s*", "", segment.strip())
        cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
        cleaned = " ".join(cleaned.split())
        if not cleaned:
            continue

        speaker, separator, body = cleaned.partition(":")
        if separator and speaker and len(speaker) <= 18:
            speaker_name = speaker.strip()
            if speaker_name.lower() in {"旁白", "narrator"}:
                continue
            entries.append(f"{speaker_name}：{body.strip()}")
        else:
            entries.append(cleaned)

    return _clip_preserving_lines("\n\n".join(entries), limit)


def _memory_graph_agents() -> list[dict]:
    agents: list[dict] = []
    for agent_name in get_agent_names(include_narrator=False):
        episodes = read_memory_jsonl(agent_name)
        understandings = read_understandings(agent_name)
        edge_count = sum(len(u.linked_episodes) for u in understandings.values())
        agents.append(
            {
                "name": agent_name,
                "display_name": _get_agent_display_name(agent_name),
                "episode_count": len(episodes),
                "understanding_count": len(understandings),
                "edge_count": edge_count,
            }
        )
    return agents


def _episode_node(agent_name: str, episode: EpisodeMemory, index: int) -> dict:
    episode_key = episode.id or f"row-{index}"
    label_source = episode.title or episode.content or episode_key
    full_source = f"{episode.date} · {label_source}" if episode.date else label_source
    label = _clip_text(full_source, _MEMORY_GRAPH_LABEL_LIMIT)
    return {
        "id": f"episode:{episode_key}",
        "label": label,
        "group": "episode",
        "value": max(2, episode.importance),
        "meta": {
            "id": episode_key,
            "agent": agent_name,
            "type": "episode",
            "type_label": "Episode",
            "title": episode.title or "未命名记忆",
            "date": episode.date,
            "time": episode.time,
            "location": episode.location,
            "participants": episode.participants,
            "keywords": episode.keywords,
            "importance": episode.importance,
            "content": episode.content,
            "content_preview": _clip_text(episode.content, _MEMORY_GRAPH_DETAIL_LIMIT),
            "raw_dialogue_preview": _format_raw_dialogue_preview(
                episode.raw_dialogue, _MEMORY_GRAPH_RAW_LIMIT
            ),
        },
    }


def _understanding_node(
    agent_name: str,
    understanding: Understanding,
    index: int,
) -> dict:
    understanding_key = understanding.id or f"row-{index}"
    label_source = understanding.subject or understanding.content or understanding_key
    return {
        "id": f"understanding:{understanding_key}",
        "label": _clip_text(label_source, _MEMORY_GRAPH_LABEL_LIMIT),
        "group": "understanding",
        "value": max(3, len(understanding.linked_episodes)),
        "meta": {
            "id": understanding_key,
            "agent": agent_name,
            "type": "understanding",
            "type_label": "Understanding",
            "title": understanding.subject or "未命名理解",
            "keywords": understanding.keywords,
            "linked_episodes": understanding.linked_episodes,
            "content": understanding.content,
            "content_preview": _clip_text(understanding.content, _MEMORY_GRAPH_DETAIL_LIMIT),
            "history": [entry.model_dump() for entry in understanding.history],
        },
    }


def _missing_episode_node(agent_name: str, episode_id: str) -> dict:
    short_id = _clip_text(episode_id, 10)
    return {
        "id": f"episode:{episode_id}",
        "label": f"缺失 · {short_id}",
        "group": "missing_episode",
        "value": 1,
        "meta": {
            "id": episode_id,
            "agent": agent_name,
            "type": "missing_episode",
            "type_label": "Missing Episode",
            "title": "缺失 Episode",
            "keywords": [],
            "content": f"understanding.jsonl 引用了这个 episode id，但当前 memory.jsonl 中没有对应记录：{episode_id}",
            "content_preview": "",
        },
    }


def _build_memory_graph(agent_name: str, display_name: str) -> dict:
    episodes = read_memory_jsonl(agent_name)
    understandings = list(read_understandings(agent_name).values())

    nodes: list[dict] = []
    edges: list[dict] = []
    episode_node_ids: set[str] = set()

    for index, episode in enumerate(episodes):
        node = _episode_node(agent_name, episode, index)
        nodes.append(node)
        episode_node_ids.add(node["id"])

    missing_episode_node_ids: set[str] = set()
    for index, understanding in enumerate(understandings):
        understanding_node = _understanding_node(agent_name, understanding, index)
        nodes.append(understanding_node)
        for link_index, episode_id in enumerate(understanding.linked_episodes):
            if not episode_id:
                continue
            episode_node_id = f"episode:{episode_id}"
            if episode_node_id not in episode_node_ids and episode_node_id not in missing_episode_node_ids:
                nodes.append(_missing_episode_node(agent_name, episode_id))
                missing_episode_node_ids.add(episode_node_id)
            edges.append(
                {
                    "id": f"{understanding_node['id']}->{episode_node_id}:{link_index}",
                    "from": understanding_node["id"],
                    "to": episode_node_id,
                }
            )

    return {
        "selected_agent": agent_name,
        "selected_display_name": display_name,
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "episode_count": len(episodes),
            "understanding_count": len(understandings),
            "edge_count": len(edges),
            "missing_episode_count": len(missing_episode_node_ids),
        },
    }


def _sse_event(event_type: str, data: dict) -> str:
    """格式化单条 SSE 事件。"""
    payload = json.dumps({"type": event_type, **data}, ensure_ascii=False)
    return f"data: {payload}\n\n"


async def _settle_pending_state_update(*, cancel: bool = False) -> None:
    """结清后台 state_updater 任务。cancel=True 时先取消（用于重置/读档）。"""
    global _pending_state_update_task
    task = _pending_state_update_task
    if task is None:
        return
    _pending_state_update_task = None
    if cancel and not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        routing_logger.info("[state_updater] 后台任务已取消")


def _start_state_update() -> None:
    global _pending_state_update_task
    _pending_state_update_task = asyncio.create_task(narrator.update_state())


# =============================================================================
# 启动初始化
# =============================================================================


@app.on_event("startup")
async def startup() -> None:
    setup_logfire()
    initialize_conversation_agents()


# =============================================================================
# 静态文件 & 页面
# =============================================================================

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# =============================================================================
# /api/init
# =============================================================================


@app.get("/api/init")
async def api_init() -> JSONResponse:
    """返回初始状态：是否有存档、最近历史、最近选项。"""
    has_save = has_existing_save()
    recent: list[dict] = []
    last_choices: list[str] = []

    if has_save:
        raw = load_conversation_history(limit=_RECENT_HISTORY_LIMIT)
        for msg in raw:
            role = msg.get("role", "")
            content = msg.get("content", "").strip()
            if content:
                recent.append(
                    {
                        "role": role,
                        "author": _get_agent_display_name(role) if role else "",
                        "content": content,
                    }
                )
        last_choices = _load_last_choices()

    return JSONResponse({"has_save": has_save, "recent": recent, "last_choices": last_choices})


# =============================================================================
# /api/stories
# =============================================================================


@app.get("/api/stories")
async def api_stories() -> JSONResponse:
    """列出可选故事模板。"""
    stories = [
        {
            "id": "school",
            "label": "私立城川中学",
            "title": "私立城川中学",
            "tagline": "青春校园",
            "summary": "夏季午后的教室、走廊与操场里，关系会在一次次对话和试探中慢慢偏移。",
        },
        {
            "id": "modern",
            "label": "不期而遇",
            "title": "不期而遇",
            "tagline": "现代都市",
            "summary": "在城市日常与偶然重逢之间，让暧昧、克制和误差一点点累积成新的局面。",
        },
    ]
    return JSONResponse({"stories": stories})


# =============================================================================
# /api/memory-graph
# =============================================================================


@app.get("/api/memory-graph")
async def api_memory_graph(agent: str | None = None) -> JSONResponse:
    """返回指定角色的 understanding ↔ episode 可视化数据。"""
    agents = _memory_graph_agents()
    agent_names = {item["name"] for item in agents}
    if not agents:
        return JSONResponse(
            {
                "agents": [],
                "selected_agent": "",
                "selected_display_name": "",
                "nodes": [],
                "edges": [],
                "stats": {
                    "episode_count": 0,
                    "understanding_count": 0,
                    "edge_count": 0,
                    "missing_episode_count": 0,
                },
            }
        )

    selected_agent = agent or agents[0]["name"]
    if selected_agent not in agent_names:
        return JSONResponse({"detail": "角色不存在。"}, status_code=404)

    selected_display_name = next(item["display_name"] for item in agents if item["name"] == selected_agent)
    graph = _build_memory_graph(selected_agent, selected_display_name)
    return JSONResponse({"agents": agents, **graph})


# =============================================================================
# /api/new_game
# =============================================================================


class NewGameRequest(BaseModel):
    story_id: str = "school"


@app.post("/api/new_game")
async def api_new_game(req: NewGameRequest) -> JSONResponse:
    """重置并开始新游戏，返回开场内容。"""
    await _settle_pending_state_update(cancel=True)
    _clear_last_choices()
    intro_text, opening_text = await reset_game(req.story_id)
    reset_entities()
    for name in get_agent_names(include_narrator=True):
        reload_conversation_agent(name)

    opening_choices_text = load_story_file(req.story_id, "opening_choices.txt")
    choices = [line.strip() for line in opening_choices_text.splitlines() if line.strip()]
    if choices:
        _save_last_choices(choices)

    return JSONResponse({"intro": intro_text, "opening": opening_text, "choices": choices})


# =============================================================================
# /api/chat  (SSE 流式)
# =============================================================================


class ChatRequest(BaseModel):
    message: str


async def _chat_stream(user_input: str):
    """核心游戏循环，通过 SSE 逐步推送结果。"""
    await _settle_pending_state_update()

    # 0. 递增全局 turn 计数；后续写入 history / memory_draft 的所有消息都带这个 turn
    current_turn = increment_turn_counter()

    # 1. narrator 路由
    targets, scene_description, new_character_specs, is_narrator_valid = (
        await narrator.route(user_input)
    )

    # 1.5 处理 narrator 请求的新角色（孵化成功后加入 targets）
    targets, created_new_characters = await bootstrap_new_characters(
        new_character_specs, targets
    )
    is_narrator_valid = is_narrator_valid and bool(targets)
    if created_new_characters:
        for character in created_new_characters:
            yield _sse_event(
                "system",
                {
                    "title": "角色已创建",
                    "name": character.display_name,
                    "identity": character.identity,
                    "character_id": character.character_id,
                },
            )

    # 2. 广播玩家消息与旁白
    # 旁白失败时不把玩家消息写进 raw，避免下一轮上下文里残留没人回应的玩家话语。
    if is_narrator_valid:
        await message_router.broadcast_player_message(targets, user_input)
        await message_router.broadcast_agent_response("narrator", targets, scene_description)
    if scene_description:
        yield _sse_event("narrator", {"content": scene_description, "author": "旁白"})

    if not targets:
        routing_logger.info("[导演] 无角色需要回应")
        if scene_description:
            _start_state_update()
        yield _sse_event("done", {})
        return

    # 4. 顺序处理角色，每完成一个立即推送
    agent_responses: list[tuple[str, str]] = []
    for agent_name in targets:
        try:
            response = await run_agent_in_scene(agent_name, targets, user_input)
            if response:
                agent_responses.append((agent_name, response))
                yield _sse_event(
                    "agent",
                    {"content": response, "author": _get_agent_display_name(agent_name)},
                )
        except Exception as e:
            routing_logger.error(f"Agent {agent_name} 运行失败: {e}")
            yield _sse_event(
                "agent",
                {"content": f"（{_get_agent_display_name(agent_name)}暂时无法回应，请稍后再试）", "author": _get_agent_display_name(agent_name)},
            )

    # 5. 生成选项；后台并行：state_updater 维护 narrator 状态 + closure detector 检测 episode 闭合
    _start_state_update()
    asyncio.create_task(memory_consolidation_flow.detect_and_consolidate(current_turn))
    if agent_responses:
        choices = await generate_choices(scene_description, agent_responses)
        if choices:
            _save_last_choices(choices)
            yield _sse_event("choices", {"choices": choices})

    yield _sse_event("done", {})


@app.post("/api/chat")
async def api_chat(req: ChatRequest) -> StreamingResponse:
    return StreamingResponse(
        _chat_stream(req.message),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# =============================================================================
# /api/saves
# =============================================================================


@app.get("/api/saves")
async def api_list_saves() -> JSONResponse:
    """列出所有存档。"""
    saves = list_save_archives()
    return JSONResponse({"saves": saves})


# =============================================================================
# /api/save
# =============================================================================


class SaveRequest(BaseModel):
    filename: str | None = None


@app.post("/api/save")
async def api_save(req: SaveRequest | None = None) -> JSONResponse:
    """导出存档。filename 为空时新建档位，有值时覆盖该档位。"""
    try:
        await _settle_pending_state_update()
        request = req or SaveRequest()
        save_path, error_detail = await export_save_archive_with_detail(
            target_filename=request.filename
        )
        if save_path:
            return JSONResponse(
                {"ok": True, "path": save_path, "filename": Path(save_path).name}
            )
        detail = error_detail or "存档导出失败。"
        routing_logger.error("[save] /api/save 失败: %s", detail)
        return JSONResponse({"ok": False, "detail": detail}, status_code=500)
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        routing_logger.error("[save] /api/save 未捕获异常: %s\n%s", detail, traceback.format_exc())
        return JSONResponse({"ok": False, "detail": detail}, status_code=500)


# =============================================================================
# /api/load
# =============================================================================


class LoadRequest(BaseModel):
    filename: str


@app.post("/api/load")
async def api_load(req: LoadRequest) -> JSONResponse:
    """加载存档。"""
    await _settle_pending_state_update(cancel=True)
    success = await import_save_archive(req.filename)
    if success:
        reset_entities()
        for name in get_agent_names(include_narrator=True):
            reload_conversation_agent(name)
        raw = load_conversation_history(limit=_RECENT_HISTORY_LIMIT)
        recent = [
            {
                "role": m.get("role", ""),
                "author": _get_agent_display_name(m.get("role", "")) if m.get("role", "") else "",
                "content": m.get("content", "").strip(),
            }
            for m in raw
            if m.get("content", "").strip()
        ]
        last_choices = _load_last_choices()
        return JSONResponse({"ok": True, "recent": recent, "last_choices": last_choices})
    return JSONResponse({"ok": False}, status_code=500)


# =============================================================================
# /api/save/{filename} DELETE
# =============================================================================


@app.delete("/api/save/{filename}")
async def api_delete_save(filename: str) -> JSONResponse:
    """删除指定存档文件。"""
    ok = delete_save_archive(filename)
    if ok:
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "detail": "存档不存在或文件名非法。"}, status_code=404)


# =============================================================================
# /api/reset
# =============================================================================


class ResetRequest(BaseModel):
    story_id: str = "school"


@app.post("/api/reset")
async def api_reset(req: ResetRequest) -> JSONResponse:
    """重置游戏。"""
    await _settle_pending_state_update(cancel=True)
    _clear_last_choices()
    intro_text, opening_text = await reset_game(req.story_id)
    reset_entities()
    for name in get_agent_names(include_narrator=True):
        reload_conversation_agent(name)

    opening_choices_text = load_story_file(req.story_id, "opening_choices.txt")
    choices = [line.strip() for line in opening_choices_text.splitlines() if line.strip()]
    if choices:
        _save_last_choices(choices)

    return JSONResponse({"intro": intro_text, "opening": opening_text, "choices": choices})
