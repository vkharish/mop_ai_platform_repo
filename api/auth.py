"""
Optional API key authentication.

If MOP_API_KEY env var is set, every request must include:
    X-API-Key: <key>

If MOP_API_KEY is NOT set, the server runs in open/dev mode
and all requests are accepted without authentication.
"""

from __future__ import annotations

import os

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)
_CONFIGURED_KEY: str | None = os.getenv("MOP_API_KEY")


async def verify_api_key(x_api_key: str | None = Security(_API_KEY_HEADER)) -> None:
    if not _CONFIGURED_KEY:
        return  # Dev mode — no key configured, accept everything
    if x_api_key != _CONFIGURED_KEY:
        raise HTTPException(
            status_code=403,
            detail="Invalid or missing API key. Set X-API-Key header.",
        )
