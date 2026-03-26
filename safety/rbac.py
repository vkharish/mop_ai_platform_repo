"""
RBAC — Role-Based Access Control.

Roles (in ascending permission order):
  reader   — GET all endpoints
  executor — start, pause, resume, abort executions
  approver — everything executor can do + approve/reject MOPs
  admin    — everything + kill switch + RBAC config

Role assignment: configs/rbac.yaml  OR  MOP_API_KEY env var (all keys default to admin
in dev mode when no rbac.yaml exists).
"""

from __future__ import annotations

import logging
import os
from enum import Enum
from pathlib import Path
from typing import Dict, Optional

import yaml
from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

logger = logging.getLogger(__name__)

_RBAC_FILE   = Path("configs") / "rbac.yaml"
_KEY_HEADER  = APIKeyHeader(name="X-Api-Key", auto_error=False)

ROLE_LEVELS = {"reader": 0, "executor": 1, "approver": 2, "admin": 3}


class Role(str, Enum):
    READER   = "reader"
    EXECUTOR = "executor"
    APPROVER = "approver"
    ADMIN    = "admin"


def _load_rbac() -> Dict[str, str]:
    """Return {api_key: role} dict. Empty dict = dev mode (all keys → admin)."""
    if not _RBAC_FILE.exists():
        return {}
    try:
        with open(_RBAC_FILE) as f:
            data = yaml.safe_load(f) or {}
        return {k: v.get("role", "reader") for k, v in data.get("api_keys", {}).items()}
    except Exception as exc:
        logger.warning("Failed to load rbac.yaml: %s", exc)
        return {}


def _resolve_role(api_key: Optional[str]) -> Role:
    rbac = _load_rbac()
    if not rbac:
        # Dev mode — no rbac.yaml, all requests are admin
        return Role.ADMIN
    if not api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key required")
    role_str = rbac.get(api_key)
    if not role_str:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API key")
    return Role(role_str)


def require_role(minimum_role: Role):
    """FastAPI dependency factory — use as Depends(require_role(Role.EXECUTOR))."""
    async def _check(api_key: Optional[str] = Security(_KEY_HEADER)) -> Role:
        role = _resolve_role(api_key)
        if ROLE_LEVELS[role] < ROLE_LEVELS[minimum_role]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient role. Required: {minimum_role}, got: {role}",
            )
        return role
    return _check


def get_role(api_key: Optional[str]) -> Role:
    """Non-dependency version for internal use."""
    return _resolve_role(api_key)
