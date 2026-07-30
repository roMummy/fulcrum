from __future__ import annotations

from fastapi import Depends

from ..core.auth import assert_provider_authorized
from ..core.runtime import get_runtime


def get_provider_runtime(_: None = Depends(assert_provider_authorized)):
    return get_runtime()
