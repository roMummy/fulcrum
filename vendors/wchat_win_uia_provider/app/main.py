from __future__ import annotations

from fastapi import FastAPI

from .api.routes_actions import router as actions_router
from .api.routes_debug import router as debug_router
from .api.routes_health import router as health_router
from .api.routes_observe import router as observe_router
from .core.logging import configure_logging

configure_logging()

app = FastAPI(title="wechat_windows_uia_provider", version="0.1.1")
app.include_router(health_router)
app.include_router(observe_router)
app.include_router(actions_router)
app.include_router(debug_router)
