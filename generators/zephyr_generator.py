"""
Zephyr Scale CSV Generator

Produces a CSV file compatible with Zephyr Scale (formerly Zephyr Squad)
bulk test case import.

Zephyr Scale CSV format (Test Cases import):
  Required columns: Name, Status
  Optional columns: Objective, Precondition, Labels, Priority, Folder, Steps

Steps are encoded as a JSON array:
  [{"step": "...", "data": "...", "result": "..."}]

Reference: Zephyr Scale Cloud - Importing test cases from CSV
"""

from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path
from typing import List, Optional

from models.canonical import CanonicalTestModel, StepType, TestStep


# Zephyr Scale CSV column headers (Zephyr Scale Cloud format)
_HEADERS = [
    "Name",
    "Objective",
    "Precondition",
    "Status",
    "Priority",
    "Labels",
    "Component",
    "Folder",
    "Steps",
]

# Step type to Zephyr priority mapping
_PRIORITY_MAP = {
    StepType.ACTION: "Medium",
    StepType.VERIFICATION: "High",
    StepType.ROLLBACK: "Critical",
    StepType.CONFIG: "Medium",
    StepType.INFO: "Low",
}


class ZephyrGenerator:
    """
    Generates Zephyr Scale CSV for bulk test case import.

    One test case per step.  Steps within a test case are encoded
    as a JSON array in the "Steps" column.
    """

    @classmethod
    def generate(
        cls,
        model: CanonicalTestModel,
        output_dir: str,
        folder_prefix: str = "/MOPs",
        project_key: Optional[str] = None,
    ) -> str:
        """
        Generate a Zephyr Scale CSV file from a CanonicalTestModel.

        Args:
            model:         The canonical test model.
            output_dir:    Directory to write the CSV file.
            folder_prefix: Zephyr folder path prefix (default: /MOPs).
            project_key:   Optional Jira project key for test case naming.

        Returns:
            Absolute path to the generated CSV file.
        """
        os.makedirs(output_dir, exist_ok=True)
        safe_title = _safe_filename(model.document_title)
        output_path = os.path.join(output_dir, f"{safe_title}_zephyr.csv")

        rows = []
        # Group steps by section for test case organization
        sections = cls._group_by_section(model.steps)

        for section_name, section_steps in sections.items():
            for step in section_steps:
                row = cls._step_to_row(step, model, section_name, folder_prefix)
                rows.append(row)

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_HEADERS, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)

        return output_path

    @classmethod
    def _step_to_row(
        cls,
        step: TestStep,
        model: CanonicalTestModel,
        section_name: str,
        folder_prefix: str,
    ) -> dict:
        """Convert a TestStep to a Zephyr CSV row dict."""

        # Test case name
        tc_name = cls._build_tc_name(step, model)

        # Objective: step description
        objective = step.description

        # Precondition: note if this is a rollback step
        precondition = ""
        if step.is_rollback:
            precondition = "ROLLBACK STEP — execute only if main procedure fails."

        # Labels: tags + step type
        labels = list(step.tags)
        if step.step_type.value not in labels:
            labels.insert(0, step.step_type.value)
        if step.is_rollback and "rollback" not in labels:
            labels.insert(0, "rollback")
        labels_str = ", ".join(labels)

        # Priority
        priority = _PRIORITY_MAP.get(step.step_type, "Medium")
        if step.is_rollback:
            priority = "Critical"

        # Folder
        folder = f"{folder_prefix}/{_safe_folder(model.document_title)}"
        if section_name and section_name != "_default":
            folder = f"{folder}/{_safe_folder(section_name)}"

        # Steps JSON array
        zephyr_steps = cls._build_zephyr_steps(step)
        steps_json = json.dumps(zephyr_steps, ensure_ascii=False)

        return {
            "Name": tc_name,
            "Objective": objective,
            "Precondition": precondition,
            "Status": "Draft",
            "Priority": priority,
            "Labels": labels_str,
            "Component": cls._detect_component(step),
            "Folder": folder,
            "Steps": steps_json,
        }

    @classmethod
    def _build_tc_name(cls, step: TestStep, model: CanonicalTestModel) -> str:
        """Build a descriptive test case name."""
        prefix = f"TC-{step.sequence:03d}"
        desc = step.description[:80].strip()
        return f"{prefix}: {desc}"

    @classmethod
    def _build_zephyr_steps(cls, step: TestStep) -> List[dict]:
        """
        Build Zephyr step objects for a TestStep.

        Each CLI command becomes its own Zephyr step row.
        If no commands, the step description is the action.
        """
        zephyr_steps = []

        if step.commands:
            for cmd in step.commands:
                zephyr_steps.append({
                    "step": cmd.raw,
                    "data": _build_step_data(cmd),
                    "result": step.expected_output or "Command executes without errors.",
                })
        else:
            # Non-CLI step (info, config block, etc.)
            zephyr_steps.append({
                "step": step.description,
                "data": step.raw_text if step.raw_text != step.description else "",
                "result": step.expected_output or "",
            })

        return zephyr_steps

    @classmethod
    def _group_by_section(cls, steps: List[TestStep]) -> dict:
        """Group steps by their section, preserving order."""
        groups: dict = {}
        for step in steps:
            key = step.section or "_default"
            if key not in groups:
                groups[key] = []
            groups[key].append(step)
        return groups

    @classmethod
    def _detect_component(cls, step: TestStep) -> str:
        """Infer a Jira component from step tags or commands."""
        vendors = {cmd.vendor for cmd in step.commands if cmd.vendor and cmd.vendor != "generic"}
        if vendors:
            return ", ".join(sorted(vendors))
        protocols = {cmd.protocol for cmd in step.commands if cmd.protocol}
        if protocols:
            return ", ".join(sorted(protocols))
        return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w\-]", "_", name).strip("_")


def _safe_folder(name: str) -> str:
    return re.sub(r"[^\w\s\-]", "", name).strip()


def _build_step_data(cmd) -> str:
    """Build metadata annotation for a CLI command."""
    parts = []
    if cmd.vendor and cmd.vendor != "generic":
        parts.append(f"Vendor: {cmd.vendor}")
    if cmd.protocol:
        parts.append(f"Protocol: {cmd.protocol}")
    if cmd.mode:
        parts.append(f"Mode: {cmd.mode}")
    return " | ".join(parts)
