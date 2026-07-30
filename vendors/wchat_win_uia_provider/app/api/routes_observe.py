from __future__ import annotations

from fastapi import APIRouter, Depends

from ..core.runtime import ProviderRuntime
from ..core.schemas import WatcherControlResponse
from .dependencies import get_provider_runtime

router = APIRouter(tags=["observe"])


@router.get("/snapshot")
def snapshot(runtime: ProviderRuntime = Depends(get_provider_runtime)) -> dict:
    return runtime.collect_snapshot()


@router.get("/sessions")
def sessions(runtime: ProviderRuntime = Depends(get_provider_runtime)) -> dict:
    return {"ok": True, "items": runtime.list_sessions()}


@router.get("/sessions/unread")
def unread_sessions(chat_type_hint: str | None = None, runtime: ProviderRuntime = Depends(get_provider_runtime)) -> dict:
    return {"ok": True, "items": runtime.list_unread_sessions(chat_type_hint=chat_type_hint)}


@router.get("/current-chat")
def current_chat(runtime: ProviderRuntime = Depends(get_provider_runtime)) -> dict:
    return {"ok": True, "conversation": runtime.get_current_chat(), "messages": runtime.get_current_messages()}


@router.get("/current-chat/incremental")
def current_chat_incremental(mark_as_seen: bool = True, runtime: ProviderRuntime = Depends(get_provider_runtime)) -> dict:
    return runtime.get_current_chat_incremental(mark_as_seen=mark_as_seen)


@router.get("/watcher/status")
def watcher_status(runtime: ProviderRuntime = Depends(get_provider_runtime)) -> dict:
    return runtime.watcher_status()


@router.post("/watcher/start", response_model=WatcherControlResponse)
def watcher_start(runtime: ProviderRuntime = Depends(get_provider_runtime)) -> WatcherControlResponse:
    return runtime.start_watcher()


@router.post("/watcher/stop", response_model=WatcherControlResponse)
def watcher_stop(runtime: ProviderRuntime = Depends(get_provider_runtime)) -> WatcherControlResponse:
    return runtime.stop_watcher()
