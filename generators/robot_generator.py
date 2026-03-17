"""
Robot Framework Generator

Produces .robot files from the CanonicalTestModel.

Generated test structure:
  - One .robot file per document
  - One test case per step
  - Keywords use SSHLibrary to execute CLI commands on network devices
  - Variables file referenced for host/credentials (not embedded)
  - Tags map to step types and protocols for selective test execution

Usage example:
  robot --variable HOST:192.0.2.1 --variable USERNAME:admin output/bgp_mop.robot
"""

from __future__ import annotations

import os
import re
import textwrap
from pathlib import Path
from typing import List, Optional

from models.canonical import ActionType, CanonicalTestModel, StepType, TestStep


# Robot Framework indentation unit
_INDENT = "    "  # 4 spaces (RF standard)

# SSHLibrary timeout for command execution
_CMD_TIMEOUT = "30s"


class RobotGenerator:
    """Generates Robot Framework .robot test files."""

    @classmethod
    def generate(
        cls,
        model: CanonicalTestModel,
        output_dir: str,
        host_variable: str = "${HOST}",
        username_variable: str = "${USERNAME}",
        password_variable: str = "${PASSWORD}",
    ) -> str:
        """
        Generate a .robot file from a CanonicalTestModel.

        Args:
            model:             The canonical test model.
            output_dir:        Directory to write the .robot file.
            host_variable:     Robot variable for the target device hostname/IP.
            username_variable: Robot variable for SSH username.
            password_variable: Robot variable for SSH password.

        Returns:
            Absolute path to the generated .robot file.
        """
        os.makedirs(output_dir, exist_ok=True)
        safe_title = _safe_filename(model.document_title)
        output_path = os.path.join(output_dir, f"{safe_title}.robot")

        sections = [
            cls._build_settings_section(model),
            cls._build_variables_section(host_variable, username_variable, password_variable),
            cls._build_test_cases_section(model),
            cls._build_keywords_section(),
        ]

        content = "\n\n".join(sections)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)

        return output_path

    # ------------------------------------------------------------------
    # Section builders
    # ------------------------------------------------------------------

    @classmethod
    def _build_settings_section(cls, model: CanonicalTestModel) -> str:
        lines = [
            "*** Settings ***",
            f"Documentation    Auto-generated Robot Framework tests for: {model.document_title}",
            f"...              Source: {os.path.basename(model.source_file)}",
            f"...              MOP Structure: {model.mop_structure}",
            "Library          SSHLibrary",
            "Library          String",
            "Library          Collections",
            "Suite Setup      Open SSH Connection",
            "Suite Teardown   Close All Connections",
        ]
        return "\n".join(lines)

    @classmethod
    def _build_variables_section(
        cls,
        host_var: str,
        user_var: str,
        pass_var: str,
    ) -> str:
        lines = [
            "*** Variables ***",
            f"${{HOST}}          ${{ENV_HOST}}",
            f"${{USERNAME}}      ${{ENV_USERNAME}}",
            f"${{PASSWORD}}      ${{ENV_PASSWORD}}",
            f"${{PROMPT}}        #",
            f"${{TIMEOUT}}       {_CMD_TIMEOUT}",
        ]
        return "\n".join(lines)

    @classmethod
    def _build_test_cases_section(cls, model: CanonicalTestModel) -> str:
        lines = ["*** Test Cases ***"]

        current_section = None
        for step in model.steps:
            # Insert section comment when section changes
            if step.section and step.section != current_section:
                current_section = step.section
                lines.append(f"\n# {'=' * 60}")
                lines.append(f"# Section: {step.section}")
                lines.append(f"# {'=' * 60}")

            tc_lines = cls._build_test_case(step)
            lines.extend(tc_lines)
            lines.append("")  # blank line between test cases

        return "\n".join(lines)

    @classmethod
    def _build_test_case(cls, step: TestStep) -> List[str]:
        """Build a single Robot Framework test case from a TestStep."""
        tc_name = cls._tc_name(step)
        lines = [tc_name]

        # Documentation
        doc = step.description.replace("\\", "\\\\")
        lines.append(f"{_INDENT}[Documentation]    {doc}")

        # Tags
        tags = cls._build_tags(step)
        if tags:
            lines.append(f"{_INDENT}[Tags]    {tags}")

        # Body — depends on step type
        if step.step_type == StepType.INFO:
            lines.append(f"{_INDENT}Log    {_escape_rf(step.description)}    INFO")

        elif step.commands:
            for cmd in step.commands:
                var_name = cls._var_name_from_cmd(cmd.raw)
                lines.append(
                    f"{_INDENT}${{{var_name}}}=    Execute CLI Command    "
                    f"{_escape_rf(cmd.raw)}"
                )

                # Add verification if we have expected output
                if step.expected_output and step.step_type in (
                    StepType.VERIFICATION, StepType.ACTION
                ):
                    lines.append(
                        f"{_INDENT}Verify Command Output    "
                        f"${{{var_name}}}    "
                        f"{_escape_rf(step.expected_output)}"
                    )
        else:
            # No commands — log the step as a manual action placeholder
            lines.append(
                f"{_INDENT}Log    MANUAL STEP: {_escape_rf(step.description)}    WARN"
            )

        return lines

    @classmethod
    def _build_keywords_section(cls) -> str:
        return textwrap.dedent("""\
        *** Keywords ***
        Open SSH Connection
            [Documentation]    Open SSH session to the target device.
            Open Connection    ${HOST}    timeout=${TIMEOUT}
            Login              ${USERNAME}    ${PASSWORD}
            Set Client Configuration    prompt=${PROMPT}

        Execute CLI Command
            [Documentation]    Execute a CLI command and return output.
            [Arguments]        ${command}
            ${output}=         Execute Command    ${command}    return_stdout=True
            Log                ${output}
            [Return]           ${output}

        Verify Command Output
            [Documentation]    Verify that command output contains expected text.
            [Arguments]        ${output}    ${expected}
            Should Contain     ${output}    ${expected}
            ...    msg=Expected '${expected}' not found in command output

        Verify No Error In Output
            [Documentation]    Verify command output does not contain error indicators.
            [Arguments]        ${output}
            Should Not Contain    ${output}    Error
            Should Not Contain    ${output}    Invalid
            Should Not Contain    ${output}    %
        """)

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
        """Generate a Robot variable name from a CLI command."""
        # Take the first two words, strip special chars
        words = re.sub(r"[^\w\s]", "", cmd).split()[:3]
        return "_".join(w.lower() for w in words) or "output"


def _escape_rf(text: str) -> str:
    """Escape Robot Framework special characters in a string."""
    return text.replace("\\", "\\\\").replace("$", "\\$").replace("@", "\\@")


def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w\-]", "_", name).strip("_")
