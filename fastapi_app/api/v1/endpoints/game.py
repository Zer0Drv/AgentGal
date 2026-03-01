from fastapi import APIRouter, Depends, HTTPException

from fastapi_app.core import security
from fastapi_app.models.user import User
from fastapi_app.schemas.game import (
    EndGameResponse,
    ListSavesResponse,
    LoadGameRequest,
    LoadGameResponse,
    ResetGameResponse,
    SaveGameResponse,
    StartActionRequest,
    StartOptionsResponse,
    StartGameResponse,
)
from fastapi_app.services.game_service import (
    end_game,
    get_start_options,
    list_saves,
    load_game,
    reset_current_game,
    save_game,
    start_game,
)

router = APIRouter(prefix="/game", tags=["game"])


@router.get("/start/options", response_model=StartOptionsResponse)
async def game_start_options(
    _: User = Depends(security.check_token),
) -> StartOptionsResponse:
    return await get_start_options()


@router.post("/start", response_model=StartGameResponse)
async def game_start(
    payload: StartActionRequest,
    _: User = Depends(security.check_token),
) -> StartGameResponse:
    return await start_game(payload.action)


@router.post("/save", response_model=SaveGameResponse)
async def game_save(
    _: User = Depends(security.check_token),
) -> SaveGameResponse:
    result = await save_game()
    if not result.success:
        raise HTTPException(status_code=500, detail=result.detail)
    return result


@router.get("/saves", response_model=ListSavesResponse)
async def game_list_saves(
    _: User = Depends(security.check_token),
) -> ListSavesResponse:
    return list_saves()


@router.post("/load", response_model=LoadGameResponse)
async def game_load(
    payload: LoadGameRequest,
    _: User = Depends(security.check_token),
) -> LoadGameResponse:
    result = await load_game(payload.save_filename)
    if not result.success:
        raise HTTPException(status_code=500, detail=result.detail)
    return result


@router.post("/reset", response_model=ResetGameResponse)
async def game_reset(
    _: User = Depends(security.check_token),
) -> ResetGameResponse:
    return await reset_current_game()


@router.post("/end", response_model=EndGameResponse)
async def game_end(
    _: User = Depends(security.check_token),
) -> EndGameResponse:
    return await end_game()
