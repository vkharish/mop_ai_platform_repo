"""
Validation Agent — verifies step outputs against expected patterns.

Two modes of validation:
  1. Passive (output matching): checks actual_output against expected_output
     regex / substring. No additional device commands sent.
  2. Active (re-query): sends validation_rules commands to the device and
     checks each result against its expect_pattern.

The agent is stateless — it reads the step definition and returns a result
without touching state_manager directly. Callers (ExecutionAgent) decide
how to persist the result.

Usage:
    from execution_engine.validation_agent import ValidationAgent
    result = ValidationAgent().validate(step, actual_output=output, driver=driver)
    if not result.passed:
        for e in result.errors:
            logger.error(e)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from models.canonical import TestStep, ActionType
from device_layer.device_driver import DeviceDriver, DeviceCommandError

logger = logging.getLogger(__name__)

# Patterns that always indicate a device error regardless of expected output
_ERROR_PATTERNS = re.compile(
    r"\b(error|invalid|fail(ed)?|not\s+found|% unknown|% bad|% incomplete"
    r"|% ambiguous|connection refused|timed?\s*out|syntax error"
    r"|permission denied|not\s+permitted)\b",
    re.IGNORECASE,
)


@dataclass
class ValidationResult:
    passed:  bool
    errors:  List[str] = field(default_factory=list)
    details: List[str] = field(default_factory=list)   # informational notes


class ValidationAgent:
    """
    Validates a step's outcome via passive output matching and/or active
    re-query commands.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(
        self,
        step: TestStep,
        actual_output: str,
        driver: Optional[DeviceDriver] = None,
        execution_id: str = "",
    ) -> ValidationResult:
        """
        Run all applicable validation checks for a step.

        Args:
            step:          The TestStep definition (expected_output, validation_rules).
            actual_output: The raw text returned by the device for the main command.
            driver:        Live device driver for active re-query checks (optional).
            execution_id:  For log correlation.

        Returns:
            ValidationResult with passed flag and list of errors.
        """
        errors: List[str] = []
        details: List[str] = []
        tag = f"[{execution_id}:{step.step_id}]"

        # Verification / observe steps need validation; action / rollback steps
        # only get error-pattern checks.
        is_verify_step = step.action_type in (ActionType.VERIFY, ActionType.OBSERVE)

        # 1. Error pattern scan (all step types)
        if actual_output:
            error_match = _ERROR_PATTERNS.search(actual_output)
            if error_match:
                errors.append(
                    f"Device error indicator in output: '{error_match.group(0)}'"
                )
                logger.warning("%s Error indicator: %s", tag, error_match.group(0))

        # 2. Expected output match (verification steps only)
        if is_verify_step and step.expected_output:
            matched = self._match_expected(step.expected_output, actual_output)
            if matched:
                details.append(f"Expected output matched: '{step.expected_output}'")
                logger.debug("%s Expected output matched", tag)
            else:
                errors.append(
                    f"Expected output not found: '{step.expected_output}'"
                )
                logger.warning("%s Expected output not found", tag)

        # 3. Active re-query via validation_rules (requires live driver)
        if step.validation_rules and driver:
            rule_errors, rule_details = self._run_validation_rules(
                step, driver, tag
            )
            errors.extend(rule_errors)
            details.extend(rule_details)

        passed = len(errors) == 0
        if passed:
            logger.debug("%s Validation passed", tag)
        else:
            logger.warning("%s Validation FAILED (%d error(s))", tag, len(errors))

        return ValidationResult(passed=passed, errors=errors, details=details)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _match_expected(self, expected: str, actual: str) -> bool:
        """
        Match expected against actual using:
          1. Regex (if expected looks like a regex — contains special chars)
          2. Case-insensitive substring
        """
        if not actual:
            return False
        # Try regex first (if it compiles and has non-literal chars)
        if any(c in expected for c in r".*+?[](){}^$|\\"):
            try:
                return bool(re.search(expected, actual, re.IGNORECASE | re.DOTALL))
            except re.error:
                pass
        # Plain substring match
        return expected.lower() in actual.lower()

    def _run_validation_rules(
        self,
        step: TestStep,
        driver: DeviceDriver,
        tag: str,
    ) -> tuple[List[str], List[str]]:
        """Execute each validation_rule command and check its output."""
        errors: List[str] = []
        details: List[str] = []

        for rule in step.validation_rules:
            try:
                output = driver.execute(rule.cmd, timeout_s=30.0)
                matched = self._match_expected(rule.expect_pattern, output)
                if matched:
                    details.append(f"Rule '{rule.cmd}' matched '{rule.expect_pattern}'")
                    logger.debug("%s Rule OK: %s", tag, rule.cmd)
                else:
                    errors.append(
                        f"Validation rule '{rule.cmd}' did not match "
                        f"'{rule.expect_pattern}'"
                    )
                    logger.warning("%s Rule FAILED: %s", tag, rule.cmd)
            except DeviceCommandError as exc:
                errors.append(f"Validation rule command error: {exc}")
                logger.error("%s Rule error for '%s': %s", tag, rule.cmd, exc)

        return errors, details
