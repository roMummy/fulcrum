from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends

from ..core.config import settings
from ..core.runtime import ProviderRuntime
from .dependencies import get_provider_runtime

router = APIRouter(prefix="/events", tags=["debug"])


@router.get("/recent")
def recent_events(runtime: ProviderRuntime = Depends(get_provider_runtime)) -> dict:
    return {"ok": True, "items": runtime.recent_events()}


@router.get("/provider-log")
def provider_log(lines: int = 100) -> dict:
    log_path = Path(settings.log_dir) / settings.runtime_log_file
    if not log_path.exists():
        return {"ok": False, "path": str(log_path), "items": []}
    items = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return {"ok": True, "path": str(log_path), "items": items[-max(lines, 1):]}


@router.get("/session-targets")
def session_targets(session_title: str, runtime: ProviderRuntime = Depends(get_provider_runtime)) -> dict:
    items = runtime.navigator.inspect_session_targets(session_title)
    return {
        "ok": True,
        "session_title": session_title,
        "current_chat_title": runtime.get_current_chat()["title"] if runtime.get_current_chat() else None,
        "items": [runtime.navigator._serialize_candidate(item) for item in items],
    }


@router.get("/message-list-tree")
def message_list_tree(
    max_depth: int = 4,
    max_children: int = 20,
    runtime: ProviderRuntime = Depends(get_provider_runtime),
) -> dict:
    return runtime.debug_message_list_tree(max_depth=max_depth, max_children=max_children)


@router.get("/message-page-descendants")
def message_page_descendants(limit: int = 200, runtime: ProviderRuntime = Depends(get_provider_runtime)) -> dict:
    return runtime.debug_message_page_descendants(limit=limit)


@router.get("/message-contexts")
def message_contexts(limit: int = 20, runtime: ProviderRuntime = Depends(get_provider_runtime)) -> dict:
    return runtime.debug_message_contexts(limit=limit)
