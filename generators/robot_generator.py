"""
Robot Framework Generator

Produces .robot files from the CanonicalTestModel.

Failure handling:
  - Every implementation/config step checks output for device error indicators
    (%, Error, Invalid, timeout) immediately after execution.
  - failure_strategy=ABORT (default): Robot stops the upgrade on first failure.
  - failure_strategy=CONTINUE: steps tagged robot:continue-on-failure.
  - failure_strategy=ROLLBACK_ALL / ROLLBACK_GROUP: Suite Teardown auto-triggers
    the generated rollback procedure when any implementation step fails.
  - Rollback steps are isolated in a dedicated keyword and never run during
    normal execution — only on failure via Suite Teardown.
  - Pre-check steps capture baseline output into suite variables for
    post-check comparison.

Usage:
  robot --variable HOST:192.0.2.1 --variable USERNAME:admin --variable PASSWORD:secret bgp_mop.robot
"""

from __future__ import annotations

import os
import re
import textwrap
from pathlib import Path
from typing import List

from models.canonical import (
    ActionType,
    CanonicalTestModel,
    FailureStrategy,
    StepType,
    TestStep,
)

_INDENT = "    "
_CMD_TIMEOUT = "30s"


class RobotGenerator:

    @classmethod
    def generate(
        cls,
        model: CanonicalTestModel,
        output_dir: str,
        host_variable: str = "${HOST}",
        username_variable: str = "${USERNAME}",
        password_variable: str = "${PASSWORD}",
    ) -> str:
        os.makedirs(output_dir, exist_ok=True)
        safe_title = _safe_filename(model.document_title)
        output_path = os.path.join(output_dir, f"{safe_title}.robot")

        strategy = model.failure_strategy or FailureStrategy.ABORT
        rollback_steps = [s for s in model.steps if s.is_rollback]
        has_rollback = bool(rollback_steps)
        auto_rollback = strategy in (
            FailureStrategy.ROLLBACK_ALL, FailureStrategy.ROLLBACK_GROUP
        )

        sections = [
            cls._build_settings_section(model, auto_rollback, has_rollback),
            cls._build_variables_section(),
            cls._build_test_cases_section(model, strategy),
            cls._build_keywords_section(model, rollback_steps, auto_rollback),
        ]

        content = "\n\n".join(sections)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)

        return output_path

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    @classmethod
    def _build_settings_section(
        cls,
        model: CanonicalTestModel,
        auto_rollback: bool,
        has_rollback: bool,
    ) -> str:
        strategy = model.failure_strategy or FailureStrategy.ABORT
        teardown = (
            "Suite Teardown   Run Keywords    Run Keyword If Any Tests Failed    Execute Rollback Procedure\n"
            "...              AND    Close All Connections"
            if auto_rollback and has_rollback
            else "Suite Teardown   Close All Connections"
        )
        lines = [
            "*** Settings ***",
            f"Documentation    Auto-generated Robot Framework tests for: {model.document_title}",
            f"...              Source: {os.path.basename(model.source_file)}",
            f"...              MOP Structure: {model.mop_structure}",
            f"...              Failure Strategy: {strategy.value.upper()}",
            "Library          SSHLibrary",
            "Library          String",
            "Library          Collections",
            "Suite Setup      Open SSH Connection",
            teardown,
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Variables
    # ------------------------------------------------------------------

    @classmethod
    def _build_variables_section(cls) -> str:
        return "\n".join([
            "*** Variables ***",
            "${HOST}              ${ENV_HOST}",
            "${USERNAME}          ${ENV_USERNAME}",
            "${PASSWORD}          ${ENV_PASSWORD}",
            "${PROMPT}            #",
            f"${{TIMEOUT}}          {_CMD_TIMEOUT}",
            "${ABORT_ON_ERROR}    ${TRUE}",
        ])

    # ------------------------------------------------------------------
    # Test Cases
    # ------------------------------------------------------------------

    @classmethod
    def _build_test_cases_section(
        cls, model: CanonicalTestModel, strategy: FailureStrategy
    ) -> str:
        lines = ["*** Test Cases ***"]
        current_section = None
        # Only emit non-rollback steps as test cases
        for step in model.steps:
            if step.is_rollback:
                continue
            if step.section and step.section != current_section:
                current_section = step.section
                lines.append(f"\n# {'=' * 60}")
                lines.append(f"# Section: {step.section}")
                lines.append(f"# {'=' * 60}")
            lines.extend(cls._build_test_case(step, strategy))
            lines.append("")
        return "\n".join(lines)

    @classmethod
    def _build_test_case(
        cls, step: TestStep, strategy: FailureStrategy
    ) -> List[str]:
        tc_name = cls._tc_name(step)
        lines = [tc_name]

        lines.append(f"{_INDENT}[Documentation]    {step.description.replace(chr(92), chr(92)*2)}")

        # Tags — add robot:continue-on-failure for CONTINUE strategy on non-critical steps
        tags = cls._build_tags(step)
        if strategy == FailureStrategy.CONTINUE and not step.is_rollback:
            tags += "    robot:continue-on-failure"
        if tags:
            lines.append(f"{_INDENT}[Tags]    {tags}")

        # Per-step teardown: log failure context
        lines.append(
            f"{_INDENT}[Teardown]    Run Keyword If Test Failed"
            f"    Log Step Failure    {_escape_rf(step.description[:60])}"
        )

        if step.step_type == StepType.INFO:
            lines.append(f"{_INDENT}Log    {_escape_rf(step.description)}    INFO")
            return lines

        if not step.commands:
            lines.append(
                f"{_INDENT}Log    MANUAL STEP: {_escape_rf(step.description)}    WARN"
            )
            return lines

        is_verify = step.action_type in (ActionType.VERIFY, ActionType.OBSERVE)
        is_precheck = (step.section or "").lower() == "pre-checks"

        for cmd in step.commands:
            var_name = cls._var_name_from_cmd(cmd.raw)

            # Pre-check steps: save to suite variable for post-check comparison
            if is_precheck:
                lines.append(
                    f"{_INDENT}${{BASELINE_{var_name.upper()}}}=    Execute CLI Command"
                    f"    {_escape_rf(cmd.raw)}"
                )
                lines.append(
                    f"{_INDENT}Set Suite Variable    "
                    f"${{BASELINE_{var_name.upper()}}}"
                )
            else:
                lines.append(
                    f"{_INDENT}${{{var_name}}}=    Execute CLI Command"
                    f"    {_escape_rf(cmd.raw)}"
                )

            output_var = (
                f"${{BASELINE_{var_name.upper()}}}" if is_precheck
                else f"${{{var_name}}}"
            )

            # Verify expected output on verification steps
            if step.expected_output and is_verify:
                lines.append(
                    f"{_INDENT}Verify Command Output    "
                    f"{output_var}    "
                    f"{_escape_rf(step.expected_output)}"
                )

            # Check for device error indicators on ALL steps (config + implementation)
            # Skip for pre-checks (show commands — some expected output patterns look like errors)
            if not is_verify and not is_precheck:
                lines.append(
                    f"{_INDENT}Verify No Device Error    {output_var}"
                )

        return lines

    # ------------------------------------------------------------------
    # Keywords
    # ------------------------------------------------------------------

    @classmethod
    def _build_keywords_section(
        cls,
        model: CanonicalTestModel,
        rollback_steps: List[TestStep],
        auto_rollback: bool,
    ) -> str:
        blocks = [
            cls._kw_open_connection(),
            cls._kw_execute_cli(),
            cls._kw_verify_output(),
            cls._kw_verify_no_device_error(),
            cls._kw_log_step_failure(),
        ]

        if rollback_steps:
            blocks.append(cls._kw_execute_rollback(rollback_steps))

        return "\n*** Keywords ***\n" + "\n\n".join(blocks)

    @classmethod
    def _kw_open_connection(cls) -> str:
        return textwrap.dedent("""\
        Open SSH Connection
            [Documentation]    Open SSH session to the target device.
            Open Connection    ${HOST}    timeout=${TIMEOUT}
            Login              ${USERNAME}    ${PASSWORD}
            Set Client Configuration    prompt=${PROMPT}""")

    @classmethod
    def _kw_execute_cli(cls) -> str:
        return textwrap.dedent("""\
        Execute CLI Command
            [Documentation]    Execute a CLI command and return output.
            [Arguments]        ${command}
            ${output}=         Execute Command    ${command}    return_stdout=True
            Log                ${output}
            [Return]           ${output}""")

    @classmethod
    def _kw_verify_output(cls) -> str:
        return textwrap.dedent("""\
        Verify Command Output
            [Documentation]    Verify that command output contains expected text.
            ...                Fails the test (and triggers abort/rollback) if not found.
            [Arguments]        ${output}    ${expected}
            Should Contain     ${output}    ${expected}
            ...    msg=VERIFICATION FAILED: Expected '${expected}' not found in output""")

    @classmethod
    def _kw_verify_no_device_error(cls) -> str:
        return textwrap.dedent("""\
        Verify No Device Error
            [Documentation]    Fail immediately if the device returned an error indicator.
            ...                Catches: %, Error, Invalid, timeout, permission denied.
            ...                This aborts the upgrade and triggers rollback if configured.
            [Arguments]        ${output}
            Should Not Match Regexp    ${output}
            ...    (?i)(^\\s*%|\\berror\\b|\\binvalid\\b|\\bfailed\\b|\\btimed?\\s*out\\b|permission denied|syntax error|not permitted)
            ...    msg=DEVICE ERROR DETECTED in output — aborting upgrade""")

    @classmethod
    def _kw_log_step_failure(cls) -> str:
        return textwrap.dedent("""\
        Log Step Failure
            [Documentation]    Called by per-step teardown when a test fails.
            [Arguments]        ${step_description}
            Log    \\n========================================    ERROR
            Log    STEP FAILED: ${step_description}    ERROR
            Log    Upgrade procedure halted. Check output above for device errors.    ERROR
            Log    ========================================    ERROR""")

    @classmethod
    def _kw_execute_rollback(cls, rollback_steps: List[TestStep]) -> str:
        """Build rollback keyword that runs all rollback steps in reverse order."""
        lines = [
            "Execute Rollback Procedure",
            f"{_INDENT}[Documentation]    Execute rollback steps in reverse order to restore network state.",
            f"{_INDENT}...                Called automatically by Suite Teardown on failure.",
            f"{_INDENT}Log    \\n==============================    WARN",
            f"{_INDENT}Log    ROLLBACK PROCEDURE STARTING    WARN",
            f"{_INDENT}Log    ==============================    WARN",
        ]

        # Reverse order for correct rollback semantics
        for step in reversed(rollback_steps):
            if not step.commands:
                continue
            for cmd in step.commands:
                var = cls._var_name_from_cmd(cmd.raw)
                lines.append(
                    f"{_INDENT}Log    Rollback: {_escape_rf(cmd.raw[:60])}    WARN"
                )
                lines.append(
                    f"{_INDENT}Run Keyword And Continue On Failure"
                    f"    Execute CLI Command    {_escape_rf(cmd.raw)}"
                )

        lines += [
            f"{_INDENT}Log    ==============================    WARN",
            f"{_INDENT}Log    ROLLBACK PROCEDURE COMPLETE    WARN",
            f"{_INDENT}Log    ==============================    WARN",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @classmethod
    def _tc_name(cls, step: TestStep) -> str:
        desc = step.description[:70].strip()
        desc = re.sub(r"[^\w\s\-\.]", " ", desc).strip()
        desc = re.sub(r"\s+", " ", desc)
        return f"Step_{step.sequence:03d}: {desc}"

    @classmethod
    def _build_tags(cls, step: TestStep) -> str:
        tags = [step.step_type.value]
        if step.is_rollback:
            tags.insert(0, "rollback")
        tags.extend(t for t in step.tags if t not in tags)
        protocols = {cmd.protocol for cmd in step.commands if cmd.protocol}
        vendors = {cmd.vendor for cmd in step.commands if cmd.vendor and cmd.vendor != "generic"}
        tags.extend(sorted(protocols))
        tags.extend(sorted(vendors))
        return "    ".join(tags)

    @classmethod
    def _var_name_from_cmd(cls, cmd: str) -> str:
        words = re.sub(r"[^\w\s]", "", cmd).split()[:3]
        return "_".join(w.lower() for w in words) or "output"


def _escape_rf(text: str) -> str:
    return text.replace("\\", "\\\\").replace("$", "\\$").replace("@", "\\@")


def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w\-]", "_", name).strip("_")
