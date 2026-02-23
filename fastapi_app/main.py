from contextlib import asynccontextmanager
from pathlib import Path
import sys

from fastapi import FastAPI
import gradio as gr
from dotenv import load_dotenv

# 兼容在 fastapi_app 目录下直接执行 `python main.py`
if __package__ is None or __package__ == "":
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
else:
    project_root = Path(__file__).resolve().parent.parent

# 关键：在导入 chat_service / agent_manager 前加载 .env
load_dotenv(project_root / ".env")

from fastapi_app.api.v1.router import api_router
from fastapi_app.core.config import settings
from fastapi_app.db.session import close_db, init_db
from fastapi_app.web.web import chat_demo_ui
from memory.consolidator import memory_consolidator


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    yield
    await memory_consolidator.close()
    await close_db()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)

app.include_router(api_router, prefix=settings.api_prefix)
app = gr.mount_gradio_app(app, chat_demo_ui, path="/web")


@app.get("/")
async def root() -> dict[str, str]:
    return {"message": "AgentGal FastAPI is running"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5001)
