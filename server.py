"""FastAPI 入口"""

# load_dotenv 必须在所有内部模块 import 之前执行，
# 否则 llm/embedding.py 等模块级常量会在 .env 加载前就固化为空值
from dotenv import load_dotenv
load_dotenv()

import asyncio
import json
import traceback
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine.agent_factory import initialize_conversation_agents, reload_conversation_agent
from engine.consolidation_flow import memory_consolidation_flow
from engine.conversation_flow import (
    call_narrator_and_route,
    generate_choices,
    run_agent_in_scene,
    run_state_updater,
)
from engine.save_manager import (
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
from storage.agent_files import read_agent_file
from storage.history import load_conversation_history
from storage.message_router import message_router

load_dotenv()

app = FastAPI(title="AgentGal")

STATIC_DIR = Path(__file__).parent / "static"
_LAST_CHOICES_FILE = CHARACTERS_DIR / "last_choices.json"
_pending_state_update_task: asyncio.Task[None] | None = None
_RECENT_HISTORY_LIMIT = 12


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
    _pending_state_update_task = asyncio.create_task(run_state_updater())


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

    # 1. narrator 路由
    targets, scene_description, is_narrator_valid = await call_narrator_and_route(user_input)

    # 2. 广播玩家消息
    await message_router.broadcast_player_message(targets, user_input)

    # 3. 推送旁白
    if scene_description:
        if is_narrator_valid:
            await message_router.broadcast_agent_response("narrator", targets, scene_description)
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
                {"content": f"[错误: {e}]", "author": _get_agent_display_name(agent_name)},
            )

    # 5. 生成选项，同时后台维护 narrator 状态
    _start_state_update()
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
    try:
        await _settle_pending_state_update()
        save_path, error_detail = await export_save_archive_with_detail()
        if save_path:
            return JSONResponse({"ok": True, "path": save_path})
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
    for name in get_agent_names(include_narrator=True):
        reload_conversation_agent(name)

    opening_choices_text = load_story_file(req.story_id, "opening_choices.txt")
    choices = [line.strip() for line in opening_choices_text.splitlines() if line.strip()]
    if choices:
        _save_last_choices(choices)

    return JSONResponse({"intro": intro_text, "opening": opening_text, "choices": choices})
