import json
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi_app.core import security
from fastapi_app.db.session import get_db
from fastapi_app.models.user import User
from fastapi_app.schemas.chat import ChatRequest, ChatResponse
from fastapi_app.services.chat_service import run_chat

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(security.check_token),
) -> ChatResponse:
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message 不能为空")
    # 强制使用当前登录用户，避免伪造 user_id
    user_id = payload.user_id or current_user.id
    if user_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权访问该用户资源")
    return await run_chat(
        user_input=message,
        db=db,
        user_id=user_id,
        conversation_id=payload.conversation_id,
    )


def _compose_stream_text(chat_resp: ChatResponse) -> str:
    chunks: list[str] = []
    if chat_resp.narrator:
        chunks.append(chat_resp.narrator)

    for reply in chat_resp.replies:
        if not reply.valid:
            continue
        chunks.append(f"{reply.agent.capitalize()}: {reply.content}")

    return "\n\n".join(chunks).strip()


@router.get("/sse")
async def chat_sse(
    query: str = Query(..., min_length=1, description="用户输入"),
    conversation_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(security.check_token),
) -> StreamingResponse:
    message = query.strip()
    if not message:
        raise HTTPException(status_code=400, detail="query 不能为空")

    async def event_generator():
        try:
            result = await run_chat(
                user_input=message,
                db=db,
                user_id=current_user.id,
                conversation_id=conversation_id,
            )
            text = _compose_stream_text(result)

            # 按固定步长切片输出，增加一点间隔，让流式效果可感知
            step = 12
            for i in range(0, len(text), step):
                delta = text[i : i + step]
                payload = {
                    "type": "text_delta",
                    "delta": {"content": delta},
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.02)

            end_payload = {"type": "stream_end"}
            yield f"data: {json.dumps(end_payload, ensure_ascii=False)}\n\n"
        except Exception as exc:
            error_payload = {
                "type": "text_delta",
                "delta": {"content": f"[错误] {exc}"},
            }
            yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
            yield 'data: {"type":"stream_end"}\n\n'

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
