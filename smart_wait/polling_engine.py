"""
Polling Engine — wait until a condition is met on a device.

Used by ValidationAgent for validation_rules with wait_for=True.

Backoff schedule: 5s → 10s → 20s → 40s → 60s (capped) → 60s → ...
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

from device_layer.device_driver import DeviceDriver

logger = logging.getLogger(__name__)

_MAX_INTERVAL_S = 60
_INITIAL_INTERVAL_S = 5
_BACKOFF_FACTOR = 2.0


@dataclass
class PollingResult:
    success:    bool
    elapsed_s:  float
    attempts:   int
    last_output: str
    matched_at: Optional[float] = None


class PollingEngine:
    """
    Polls a device command until a regex pattern matches (or doesn't match
    when negate=True), respecting a maximum wait time.
    """

    def wait_for(
        self,
        driver: DeviceDriver,
        cmd: str,
        pattern: str,
        max_wait_s: int,
        negate: bool = False,
        execution_id: str = "",
        step_id: str = "",
    ) -> PollingResult:
        """
        Execute `cmd` on `driver` repeatedly until:
          - negate=False: output matches `pattern`
          - negate=True:  output does NOT match `pattern`
        or `max_wait_s` is exceeded.
        """
        interval = _INITIAL_INTERVAL_S
        elapsed = 0.0
        attempts = 0
        last_output = ""
        regex = re.compile(pattern, re.IGNORECASE | re.DOTALL)

        logger.info("[%s:%s] Polling '%s' (max %ds, negate=%s)",
                    execution_id, step_id, cmd[:60], max_wait_s, negate)

        while elapsed < max_wait_s:
            attempts += 1
            try:
                last_output = driver.execute(cmd, timeout_s=min(30.0, max_wait_s - elapsed))
            except Exception as exc:
                logger.warning("[%s:%s] Poll attempt %d failed: %s", execution_id, step_id, attempts, exc)
                last_output = f"ERROR: {exc}"

            matched = bool(regex.search(last_output))
            condition_met = (matched and not negate) or (not matched and negate)

            if condition_met:
                logger.info("[%s:%s] Condition met after %.1fs (%d attempts)",
                            execution_id, step_id, elapsed, attempts)
                return PollingResult(
                    success=True,
                    elapsed_s=elapsed,
                    attempts=attempts,
                    last_output=last_output,
                    matched_at=elapsed,
                )

            sleep_time = min(interval, max_wait_s - elapsed)
            if sleep_time <= 0:
                break
            logger.debug("[%s:%s] Attempt %d: condition not met, sleeping %.0fs",
                         execution_id, step_id, attempts, sleep_time)
            time.sleep(sleep_time)
            elapsed += sleep_time
            interval = min(interval * _BACKOFF_FACTOR, _MAX_INTERVAL_S)

        logger.warning("[%s:%s] Polling timed out after %.1fs (%d attempts)",
                       execution_id, step_id, elapsed, attempts)
        return PollingResult(success=False, elapsed_s=elapsed, attempts=attempts, last_output=last_output)
