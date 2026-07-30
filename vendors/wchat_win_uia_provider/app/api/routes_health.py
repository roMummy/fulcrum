from __future__ import annotations

from fastapi import APIRouter, Depends

from ..core.runtime import ProviderRuntime
from .dependencies import get_provider_runtime

router = APIRouter(tags=["health"])


@router.get("/health")
def health(runtime: ProviderRuntime = Depends(get_provider_runtime)) -> dict:
    return runtime.health_report()


@router.get("/runtime")
def runtime_status(runtime: ProviderRuntime = Depends(get_provider_runtime)) -> dict:
    return runtime.runtime_report()
