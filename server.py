"""FastAPI 入口 - 替代 Chainlit app.py"""

import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine.agent_factory import initialize_conversation_agents, reload_conversation_agent
from engine.consolidation_flow import memory_consolidation_flow
from engine.conversation_flow import call_narrator_and_route, generate_choices, run_agent_in_scene
from engine.save_manager import (
    export_save_archive,
    has_existing_save,
    import_save_archive,
    list_save_archives,
    load_story_file,
    reset_game,
)
from log_config.routing import routing_logger
from shared.config import CHARACTERS_DIR, get_agent_names
from storage.history import load_conversation_history
from storage.message_router import message_router

load_dotenv()

app = FastAPI(title="AgentGal")

STATIC_DIR = Path(__file__).parent / "static"
_LAST_CHOICES_FILE = CHARACTERS_DIR / "last_choices.json"


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


def _sse_event(event_type: str, data: dict) -> str:
    """格式化单条 SSE 事件。"""
    payload = json.dumps({"type": event_type, **data}, ensure_ascii=False)
    return f"data: {payload}\n\n"


# =============================================================================
# 启动初始化
# =============================================================================


@app.on_event("startup")
async def startup() -> None:
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
    """返回初始状态：是否有存档、最近5条历史、最近选项。"""
    has_save = has_existing_save()
    recent: list[dict] = []
    last_choices: list[str] = []

    if has_save:
        raw = load_conversation_history(limit=5)
        for msg in raw:
            role = msg.get("role", "")
            content = msg.get("content", "").strip()
            if content:
                recent.append({"role": role, "content": content})
        last_choices = _load_last_choices()

    return JSONResponse({"has_save": has_save, "recent": recent, "last_choices": last_choices})


# =============================================================================
# /api/stories
# =============================================================================


@app.get("/api/stories")
async def api_stories() -> JSONResponse:
    """列出可选故事模板。"""
    stories = [
        {"id": "school", "label": "\U0001f3eb 私立城川中学 · 青春校园"},
        {"id": "modern", "label": "\U0001f306 不期而遇 · 现代都市"},
    ]
    return JSONResponse({"stories": stories})


# =============================================================================
# /api/new_game
# =============================================================================


class NewGameRequest(BaseModel):
    story_id: str = "school"


@app.post("/api/new_game")
async def api_new_game(req: NewGameRequest) -> JSONResponse:
    """重置并开始新游戏，返回开场内容。"""
    _clear_last_choices()
    intro_text, opening_text = await reset_game(req.story_id)
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
    # 1. narrator 路由
    targets, scene_description, is_narrator_valid = await call_narrator_and_route(user_input)

    # 2. 广播玩家消息
    await message_router.broadcast_player_message(targets, user_input)

    # 3. 推送旁白
    if scene_description:
        if is_narrator_valid:
            await message_router.broadcast_agent_response("narrator", targets, scene_description)
        yield _sse_event("narrator", {"content": scene_description, "author": "Narrator"})

    if not targets:
        routing_logger.info("[导演] 无角色需要回应")
        yield _sse_event("done", {})
        return

    # 4. 顺序处理角色，每完成一个立即推送
    agent_responses: list[tuple[str, str]] = []
    for agent_name in targets:
        try:
            response = await run_agent_in_scene(agent_name, targets, user_input)
            if response:
                agent_responses.append((agent_name, response))
                yield _sse_event("agent", {"content": response, "author": agent_name.capitalize()})
        except Exception as e:
            routing_logger.error(f"Agent {agent_name} 运行失败: {e}")
            yield _sse_event("agent", {"content": f"[错误: {e}]", "author": agent_name.capitalize()})

    # 5. 生成选项
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


@app.post("/api/save")
async def api_save() -> JSONResponse:
    """导出存档。"""
    save_path = await export_save_archive()
    if save_path:
        return JSONResponse({"ok": True, "path": save_path})
    return JSONResponse({"ok": False}, status_code=500)


# =============================================================================
# /api/load
# =============================================================================


class LoadRequest(BaseModel):
    filename: str


@app.post("/api/load")
async def api_load(req: LoadRequest) -> JSONResponse:
    """加载存档。"""
    success = await import_save_archive(req.filename)
    if success:
        for name in get_agent_names(include_narrator=True):
            reload_conversation_agent(name)
        raw = load_conversation_history(limit=5)
        recent = [
            {"role": m.get("role", ""), "content": m.get("content", "").strip()}
            for m in raw
            if m.get("content", "").strip()
        ]
        last_choices = _load_last_choices()
        return JSONResponse({"ok": True, "recent": recent, "last_choices": last_choices})
    return JSONResponse({"ok": False}, status_code=500)


# =============================================================================
# /api/reset
# =============================================================================


class ResetRequest(BaseModel):
    story_id: str = "school"


@app.post("/api/reset")
async def api_reset(req: ResetRequest) -> JSONResponse:
    """重置游戏。"""
    _clear_last_choices()
    intro_text, opening_text = await reset_game(req.story_id)
    for name in get_agent_names(include_narrator=True):
        reload_conversation_agent(name)

    opening_choices_text = load_story_file(req.story_id, "opening_choices.txt")
    choices = [line.strip() for line in opening_choices_text.splitlines() if line.strip()]
    if choices:
        _save_last_choices(choices)

    return JSONResponse({"intro": intro_text, "opening": opening_text, "choices": choices})
