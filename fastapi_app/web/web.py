"""Chainlit Web 客户端（对接 FastAPI API）"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import chainlit as cl
import requests
from loguru import logger


FASTAPI_BASE_URL = os.getenv("FASTAPI_BASE_URL", "http://127.0.0.1:5001")
FASTAPI_API_PREFIX = os.getenv("FASTAPI_API_PREFIX", "/api/v1")


def _api_url(path: str) -> str:
    return f"{FASTAPI_BASE_URL.rstrip('/')}{FASTAPI_API_PREFIX}{path}"


async def _api_request(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: int = 20,
) -> requests.Response:
    """在线程池执行阻塞 HTTP 请求，避免卡住 Chainlit 事件循环。"""

    def _do_request() -> requests.Response:
        with requests.Session() as session:
            session.trust_env = False
            return session.request(
                method=method,
                url=_api_url(path),
                headers=headers,
                json=json_body,
                timeout=timeout,
            )

    return await asyncio.to_thread(_do_request)


def _session_token() -> str | None:
    return cl.user_session.get("token")


def _is_logged_in() -> bool:
    return bool(_session_token())


def _has_started_game() -> bool:
    return bool(cl.user_session.get("started", False))


def _auth_headers() -> dict[str, str]:
    token = _session_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _render_recent_messages(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for msg in messages:
        author = str(msg.get("author", "")).strip()
        content = str(msg.get("content", "")).strip()
        if content:
            lines.append(f"{author or 'Unknown'}: {content}")
    return "\n".join(lines) if lines else "暂无历史回顾。"


def _compose_chat_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    narrator = str(payload.get("narrator") or "").strip()
    if narrator:
        chunks.append(f"Narrator: {narrator}")

    for reply in payload.get("replies", []) or []:
        if not reply.get("valid", False):
            continue
        agent = str(reply.get("agent", "")).strip() or "Unknown"
        content = str(reply.get("content", "")).strip()
        if content:
            chunks.append(f"{agent.capitalize()}: {content}")

    return "\n\n".join(chunks).strip() or "（未收到响应内容）"


async def _show_help() -> None:
    await cl.Message(
        content=(
            "可用命令：\n"
            "- `/register <username> <password>` 注册\n"
            "- `/login <username> <password>` 登录\n"
            "- `/start new` 新开局\n"
            "- `/start continue` 继续上次游戏\n"
            "- `/save` 导出存档\n"
            "- `/load list` 查看存档列表\n"
            "- `/load <序号>` 加载对应存档\n"
            "- `/reset` 重置当前游戏\n"
            "- `/logout` 登出\n"
            "- `/help` 查看帮助"
        )
    ).send()


async def _handle_register(parts: list[str]) -> None:
    if len(parts) != 3:
        await cl.Message(content="用法: `/register <username> <password>`").send()
        return

    _, username, password = parts
    try:
        resp = await _api_request(
            "POST",
            "/User/register",
            json_body={"username": username, "password": password},
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("success"):
            await cl.Message(content=f"注册成功：{username}").send()
            return
        await cl.Message(content=f"注册失败：{data.get('message', f'HTTP {resp.status_code}')}").send()
    except Exception as exc:
        logger.exception("注册异常")
        await cl.Message(content=f"注册异常：{exc}").send()


async def _handle_login(parts: list[str]) -> None:
    if len(parts) != 3:
        await cl.Message(content="用法: `/login <username> <password>`").send()
        return

    _, username, password = parts
    try:
        resp = await _api_request(
            "POST",
            "/User/login",
            json_body={"username": username, "password": password},
        )
        data = resp.json()
        if resp.status_code != 200 or not data.get("success"):
            await cl.Message(content=f"登录失败：{data.get('message', f'HTTP {resp.status_code}')}").send()
            return

        token = data["data"]["token"]
        cl.user_session.set("username", username)
        cl.user_session.set("token", token)
        cl.user_session.set("started", False)

        await cl.Message(content=f"登录成功：{username}").send()

        options_resp = await _api_request(
            "GET",
            "/game/start/options",
            headers=_auth_headers(),
        )
        if options_resp.status_code != 200:
            await cl.Message(content=f"已登录，但读取开局选项失败：HTTP {options_resp.status_code}").send()
            return

        options = options_resp.json()
        prompt = str(options.get("prompt", "")).strip() or "请选择开局方式。"
        preview = _render_recent_messages(options.get("recent_messages", []) or [])
        await cl.Message(content=f"{prompt}\n\n最近对话回顾：\n{preview}").send()
        await cl.Message(content="请输入 `/start new` 或 `/start continue`。").send()
    except Exception as exc:
        logger.exception("登录异常")
        await cl.Message(content=f"登录异常：{exc}").send()


async def _handle_logout() -> None:
    cl.user_session.set("username", None)
    cl.user_session.set("token", None)
    cl.user_session.set("started", False)
    await cl.Message(content="已登出。").send()


async def _handle_start(parts: list[str]) -> None:
    if len(parts) != 2 or parts[1] not in {"new", "continue"}:
        await cl.Message(content="用法: `/start new` 或 `/start continue`").send()
        return

    if not _is_logged_in():
        await cl.Message(content="请先登录：`/login <username> <password>`").send()
        return

    action = parts[1]
    try:
        resp = await _api_request(
            "POST",
            "/game/start",
            headers=_auth_headers(),
            json_body={"action": action},
            timeout=30,
        )
        if resp.status_code != 200:
            await cl.Message(content=f"开局失败：HTTP {resp.status_code}").send()
            return

        payload = resp.json()
        system_message = str(payload.get("system_message", "")).strip()
        opening = str(payload.get("opening", "")).strip()
        recent_messages = payload.get("recent_messages", []) or []

        cl.user_session.set("started", True)
        await cl.Message(content=f"已进入会话（{action}）。").send()

        if system_message:
            await cl.Message(content=system_message, author="System").send()
        if opening:
            await cl.Message(content=opening, author="Narrator").send()
        for msg in recent_messages:
            author = str(msg.get("author", "")).strip() or "Unknown"
            content = str(msg.get("content", "")).strip()
            if content:
                await cl.Message(content=content, author=author).send()
    except Exception as exc:
        logger.exception("开局异常")
        await cl.Message(content=f"开局异常：{exc}").send()


async def _handle_save() -> None:
    if not _is_logged_in():
        await cl.Message(content="请先登录。").send()
        return

    try:
        resp = await _api_request(
            "POST",
            "/game/save",
            headers=_auth_headers(),
            timeout=30,
        )
        if resp.status_code != 200:
            await cl.Message(content=f"存档失败：HTTP {resp.status_code}").send()
            return
        data = resp.json()
        detail = str(data.get("detail", "")).strip()
        save_path = str(data.get("save_path", "")).strip()
        text = f"{detail}\n{save_path}".strip()
        await cl.Message(content=text).send()
    except Exception as exc:
        logger.exception("存档异常")
        await cl.Message(content=f"存档异常：{exc}").send()


async def _handle_load(parts: list[str]) -> None:
    if not _is_logged_in():
        await cl.Message(content="请先登录。").send()
        return

    if len(parts) < 2:
        await cl.Message(content="用法: `/load list` 或 `/load <序号>`").send()
        return

    sub = parts[1].strip().lower()

    # /load list：列出存档
    if sub == "list":
        try:
            resp = await _api_request("GET", "/game/saves", headers=_auth_headers())
            if resp.status_code != 200:
                await cl.Message(content=f"获取存档列表失败：HTTP {resp.status_code}").send()
                return
            saves = resp.json().get("saves", [])
            if not saves:
                await cl.Message(content="暂无存档。").send()
                return
            lines = ["存档列表："]
            for s in saves:
                lines.append(f"{s['index']}. {s['display_time']}  ({s['filename']})")
            lines.append("\n使用 `/load <序号>` 加载对应存档。")
            await cl.Message(content="\n".join(lines)).send()
        except Exception as exc:
            logger.exception("获取存档列表异常")
            await cl.Message(content=f"获取存档列表异常：{exc}").send()
        return

    # /load <n>：按序号加载
    if not sub.isdigit():
        await cl.Message(content="用法: `/load list` 或 `/load <序号>`").send()
        return

    index = int(sub)
    try:
        # 先拉存档列表，按序号找文件名
        resp = await _api_request("GET", "/game/saves", headers=_auth_headers())
        if resp.status_code != 200:
            await cl.Message(content=f"获取存档列表失败：HTTP {resp.status_code}").send()
            return
        saves = resp.json().get("saves", [])
        match = next((s for s in saves if s["index"] == index), None)
        if not match:
            await cl.Message(content=f"序号 {index} 不存在，请用 `/load list` 查看存档列表。").send()
            return

        await cl.Message(content=f"正在读取存档：{match['display_time']}…").send()
        load_resp = await _api_request(
            "POST",
            "/game/load",
            headers=_auth_headers(),
            json_body={"save_filename": match["filename"]},
            timeout=30,
        )
        if load_resp.status_code != 200:
            await cl.Message(content=f"读档失败：HTTP {load_resp.status_code}").send()
            return
        data = load_resp.json()
        cl.user_session.set("started", True)
        await cl.Message(content=data.get("detail", "读档成功。")).send()
    except Exception as exc:
        logger.exception("读档异常")
        await cl.Message(content=f"读档异常：{exc}").send()


async def _handle_reset() -> None:
    if not _is_logged_in():
        await cl.Message(content="请先登录。").send()
        return

    try:
        resp = await _api_request(
            "POST",
            "/game/reset",
            headers=_auth_headers(),
            timeout=30,
        )
        if resp.status_code != 200:
            await cl.Message(content=f"重置失败：HTTP {resp.status_code}").send()
            return
        data = resp.json()
        cl.user_session.set("started", True)
        detail = str(data.get("detail", "")).strip()
        opening = str(data.get("opening", "")).strip()
        await cl.Message(content=detail or "已重置。").send()
        if opening:
            await cl.Message(content=opening, author="Narrator").send()
    except Exception as exc:
        logger.exception("重置异常")
        await cl.Message(content=f"重置异常：{exc}").send()


@cl.on_chat_start
async def on_chat_start() -> None:
    cl.user_session.set("username", None)
    cl.user_session.set("token", None)
    cl.user_session.set("started", False)
    await cl.Message(content="欢迎使用 AgentGal Chainlit Web。").send()
    await _show_help()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    text = (message.content or "").strip()
    if not text:
        await cl.Message(content="请输入内容。").send()
        return

    if text.startswith("/"):
        parts = text.split()
        command = parts[0].lower()
        if command == "/help":
            await _show_help()
            return
        if command == "/register":
            await _handle_register(parts)
            return
        if command == "/login":
            await _handle_login(parts)
            return
        if command == "/logout":
            await _handle_logout()
            return
        if command == "/start":
            await _handle_start(parts)
            return
        if command == "/save":
            await _handle_save()
            return
        if command == "/load":
            await _handle_load(parts)
            return
        if command == "/reset":
            await _handle_reset()
            return
        await cl.Message(content=f"未知命令：{parts[0]}，输入 `/help` 查看命令。").send()
        return

    if not _is_logged_in():
        await cl.Message(content="请先登录：`/login <username> <password>`").send()
        return

    if not _has_started_game():
        await cl.Message(content="请先开局：`/start new` 或 `/start continue`").send()
        return

    try:
        resp = await _api_request(
            "POST",
            "/chat",
            headers=_auth_headers(),
            json_body={"message": text},
            timeout=120,
        )
        if resp.status_code != 200:
            await cl.Message(content=f"聊天失败：HTTP {resp.status_code}").send()
            return
        payload = resp.json()
        output = _compose_chat_text(payload)
        await cl.Message(content=output).send()
    except Exception as exc:
        logger.exception("聊天异常")
        await cl.Message(content=f"聊天异常：{exc}").send()
