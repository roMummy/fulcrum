from __future__ import annotations

from fastapi import APIRouter, Depends

from ..core.runtime import ProviderRuntime
from ..core.schemas import (
    FetchFirstUnreadIncrementalRequest,
    SendFileRequest,
    SendTextRequest,
    SwitchFirstUnreadRequest,
    SwitchSessionRequest,
)
from .dependencies import get_provider_runtime

router = APIRouter(prefix="/actions", tags=["actions"])


@router.post("/switch-session")
def switch_session(
    payload: SwitchSessionRequest,
    runtime: ProviderRuntime = Depends(get_provider_runtime),
) -> dict:
    return runtime.switch_session(payload)


@router.post("/switch-first-unread")
def switch_first_unread(
    payload: SwitchFirstUnreadRequest,
    runtime: ProviderRuntime = Depends(get_provider_runtime),
) -> dict:
    return runtime.switch_first_unread_session(payload)


@router.post("/fetch-first-unread-incremental")
def fetch_first_unread_incremental(
    payload: FetchFirstUnreadIncrementalRequest,
    runtime: ProviderRuntime = Depends(get_provider_runtime),
) -> dict:
    return runtime.fetch_first_unread_incremental(payload)


@router.post("/send-text")
def send_text(
    payload: SendTextRequest,
    runtime: ProviderRuntime = Depends(get_provider_runtime),
) -> dict:
    return runtime.send_text(payload)


@router.post("/send-file")
def send_file(
    payload: SendFileRequest,
    runtime: ProviderRuntime = Depends(get_provider_runtime),
) -> dict:
    return runtime.send_file(payload)
