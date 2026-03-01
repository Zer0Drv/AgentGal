from game.save_manager import (
    export_save_archive,
    has_existing_save,
    import_save_archive,
    list_save_archives,
    load_recent_raw_messages,
    reset_game,
)
from memory.consolidator import memory_consolidator

from fastapi_app.schemas.game import (
    EndGameResponse,
    ListSavesResponse,
    LoadGameResponse,
    RecentMessage,
    ResetGameResponse,
    SaveGameResponse,
    SaveInfo,
    StartOptionsResponse,
    StartGameResponse,
)


def _map_recent_messages(limit: int = 5) -> list[RecentMessage]:
    recent_messages = load_recent_raw_messages(limit=limit)
    author_map = {
        "player": "User",
        "narrator": "Narrator",
    }

    mapped: list[RecentMessage] = []
    for msg in recent_messages:
        role = str(msg.get("role", "unknown"))
        content = str(msg.get("content", "")).strip()
        if not content:
            continue
        mapped.append(
            RecentMessage(
                role=role,
                author=author_map.get(role, role.capitalize()),
                content=content,
            )
        )
    return mapped


async def get_start_options() -> StartOptionsResponse:
    has_save = has_existing_save()
    if not has_save:
        return StartOptionsResponse(
            has_save=False,
            recommended_action="new",
            prompt="未检测到存档，将开始新游戏。",
            recent_messages=[],
        )
    return StartOptionsResponse(
        has_save=True,
        recommended_action="continue",
        prompt="检测到存档，是否继续上次游戏？",
        recent_messages=_map_recent_messages(limit=5),
    )


async def start_game(action: str) -> StartGameResponse:
    if action == "new":
        opening = await reset_game(show_opening=True)
        return StartGameResponse(
            mode="new",
            system_message="已重置记忆，开始新游戏。",
            opening=opening,
            recent_messages=[],
        )

    recent = _map_recent_messages(limit=5)
    if not recent:
        return StartGameResponse(
            mode="continue",
            system_message="继续上次游戏。",
            opening=None,
            recent_messages=[],
        )

    return StartGameResponse(
        mode="continue",
        system_message="继续上次游戏，以下是最近的对话回顾：",
        opening=None,
        recent_messages=recent,
    )


async def save_game() -> SaveGameResponse:
    save_path = await export_save_archive()
    if not save_path:
        return SaveGameResponse(success=False, save_path=None, detail="存档导出失败")
    return SaveGameResponse(success=True, save_path=save_path, detail="存档导出成功")


async def reset_current_game() -> ResetGameResponse:
    opening = await reset_game(show_opening=True)
    return ResetGameResponse(success=True, opening=opening, detail="游戏已重置")


def list_saves() -> ListSavesResponse:
    raw = list_save_archives()
    saves = [
        SaveInfo(index=i + 1, filename=item["filename"], display_time=item["display_time"])
        for i, item in enumerate(raw)
    ]
    return ListSavesResponse(saves=saves)


async def load_game(save_filename: str) -> LoadGameResponse:
    success = await import_save_archive(save_filename)
    if not success:
        return LoadGameResponse(success=False, detail=f"读档失败：{save_filename}")
    return LoadGameResponse(success=True, detail=f"读档成功：{save_filename}")


async def end_game() -> EndGameResponse:
    await memory_consolidator.close()
    return EndGameResponse(success=True, detail="聊天结束清理完成")
