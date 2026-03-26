"""
Idempotency Engine — checks device state before executing a step.

For each IdempotencyRule on a step:
  1. Execute check_cmd on device
  2. If output matches skip_pattern (regex) → step already applied → SKIP
  3. If output partially matches (>0 lines match but not all) → PARTIAL_STATE → fail
  4. If no match → execute normally

Non-idempotent commands (undo, no, delete, reload) bypass checks entirely.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import List

from device_layer.device_driver import DeviceDriver
from models.canonical import IdempotencyRule

logger = logging.getLogger(__name__)

_NON_IDEMPOTENT_PREFIXES = (
    "undo ", "no ", "delete ", "reload", "erase", "shutdown", "format ",
)


class IdempotencyVerdict(str, Enum):
    PROCEED       = "proceed"       # not applied yet, execute normally
    SKIP          = "skip"          # already fully applied, skip
    PARTIAL_STATE = "partial_state" # partially applied, dangerous — fail


@dataclass
class IdempotencyResult:
    verdict:     IdempotencyVerdict
    rule_fired:  str = ""   # which rule triggered the verdict
    check_output: str = ""


def check(
    rules: List[IdempotencyRule],
    driver: DeviceDriver,
    step_description: str = "",
    execution_id: str = "",
    step_id: str = "",
) -> IdempotencyResult:
    """
    Run all idempotency rules for a step. Returns the first non-PROCEED verdict,
    or PROCEED if all rules pass.
    """
    if not rules:
        return IdempotencyResult(verdict=IdempotencyVerdict.PROCEED)

    for rule in rules:
        result = _check_one(rule, driver, execution_id, step_id)
        if result.verdict != IdempotencyVerdict.PROCEED:
            logger.info(
                "[%s:%s] Idempotency verdict=%s for '%s' (rule: %s)",
                execution_id, step_id, result.verdict.value, step_description, rule.description or rule.check_cmd[:40]
            )
            return result

    return IdempotencyResult(verdict=IdempotencyVerdict.PROCEED)


def is_non_idempotent(command: str) -> bool:
    cmd = command.strip().lower()
    return any(cmd.startswith(p) for p in _NON_IDEMPOTENT_PREFIXES)


def _check_one(
    rule: IdempotencyRule,
    driver: DeviceDriver,
    execution_id: str,
    step_id: str,
) -> IdempotencyResult:
    try:
        output = driver.execute(rule.check_cmd, timeout_s=15.0)
    except Exception as exc:
        logger.warning("[%s:%s] Idempotency check_cmd failed: %s", execution_id, step_id, exc)
        return IdempotencyResult(verdict=IdempotencyVerdict.PROCEED, check_output=str(exc))

    pattern = re.compile(rule.skip_pattern, re.IGNORECASE | re.DOTALL)
    match = pattern.search(output)

    if match:
        return IdempotencyResult(
            verdict=IdempotencyVerdict.SKIP,
            rule_fired=rule.description or rule.check_cmd,
            check_output=output,
        )

    # Partial-state detection: check if any lines of expected output are present
    # but not all (heuristic: >0 and <50% of pattern lines found)
    lines = [l.strip() for l in rule.skip_pattern.split("\\n") if l.strip()]
    if len(lines) > 1:
        matched_lines = sum(1 for l in lines if re.search(l, output, re.IGNORECASE))
        if 0 < matched_lines < len(lines):
            logger.warning(
                "[%s:%s] PARTIAL STATE detected: %d/%d expected lines found",
                execution_id, step_id, matched_lines, len(lines)
            )
            return IdempotencyResult(
                verdict=IdempotencyVerdict.PARTIAL_STATE,
                rule_fired=rule.check_cmd,
                check_output=output,
            )

    return IdempotencyResult(verdict=IdempotencyVerdict.PROCEED, check_output=output)
