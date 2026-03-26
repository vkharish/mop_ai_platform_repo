"""
Maintenance Window Enforcement.

Reads the maintenance_window field from CanonicalTestModel:
    {"start": "2026-03-21T02:00:00Z", "end": "2026-03-21T04:00:00Z"}

Raises MaintenanceWindowError if execution is requested outside the window.
During execution, pauses and polls until back in window (up to grace_period_s).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_GRACE_PERIOD_S = 1800   # 30-minute grace period after window closes before hard stop
_POLL_INTERVAL_S = 60


class MaintenanceWindowError(Exception):
    pass


def parse_window(window_dict: Optional[dict]) -> Optional[tuple]:
    """Parse {"start": ISO, "end": ISO} → (start_dt, end_dt) or None."""
    if not window_dict:
        return None
    try:
        start = datetime.fromisoformat(window_dict["start"].replace("Z", "+00:00"))
        end   = datetime.fromisoformat(window_dict["end"].replace("Z", "+00:00"))
        return start, end
    except Exception as exc:
        logger.warning("Invalid maintenance_window format: %s", exc)
        return None


def check_window(window_dict: Optional[dict]) -> None:
    """
    Raise MaintenanceWindowError if now is outside the maintenance window.
    No-op if no window is defined.
    """
    window = parse_window(window_dict)
    if window is None:
        return
    start, end = window
    now = datetime.now(timezone.utc)
    if now < start:
        raise MaintenanceWindowError(
            f"Maintenance window has not started yet. Starts at {start.isoformat()}"
        )
    if now > end:
        raise MaintenanceWindowError(
            f"Maintenance window has expired. Ended at {end.isoformat()}"
        )


def is_in_window(window_dict: Optional[dict]) -> bool:
    """Return True if now is within the window (or no window defined)."""
    window = parse_window(window_dict)
    if window is None:
        return True
    start, end = window
    now = datetime.now(timezone.utc)
    return start <= now <= end


def wait_for_window(window_dict: Optional[dict], execution_id: str) -> None:
    """
    Block until we are within the maintenance window.
    Raises MaintenanceWindowError if the window never opens within grace_period_s.
    """
    window = parse_window(window_dict)
    if window is None:
        return
    start, end = window

    now = datetime.now(timezone.utc)
    if now > end:
        raise MaintenanceWindowError(
            f"[{execution_id}] Maintenance window already closed at {end.isoformat()}"
        )

    waited = 0.0
    while not is_in_window(window_dict):
        if waited >= _GRACE_PERIOD_S:
            raise MaintenanceWindowError(
                f"[{execution_id}] Timed out waiting for maintenance window to open"
            )
        logger.info("[%s] Outside maintenance window — waiting %ds (waited %.0fs so far)",
                    execution_id, _POLL_INTERVAL_S, waited)
        time.sleep(_POLL_INTERVAL_S)
        waited += _POLL_INTERVAL_S
