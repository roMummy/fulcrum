from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from .config import settings


def assert_provider_authorized(
    authorization: str | None = Header(default=None),
    x_provider_token: str | None = Header(default=None),
) -> None:
    expected = str(settings.provider_token or "").strip()
    if not expected:
        return
    bearer = ""
    if authorization:
        normalized = str(authorization or "").strip()
        if normalized.lower().startswith("bearer "):
            bearer = normalized[7:].strip()
    provided = bearer or str(x_provider_token or "").strip()
    if not provided or not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="invalid_provider_token")
