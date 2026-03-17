"""
CLI Rule Generator

Produces machine-readable CLI validation rules from the CanonicalTestModel.
These rules can be consumed by compliance engines, pre-check scripts,
or automated auditing tools.

Output format: JSON file with per-step command rules.

Rule structure per command:
  {
    "step": 1,
    "command": "show ip bgp summary",
    "vendor": "cisco",
    "protocol": "bgp",
    "mode": "exec",
    "must_contain": ["Established"],   # from expected_output
    "must_not_contain": ["Error"],
    "is_rollback": false,
    "section": "Pre-checks"
  }
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from models.canonical import CanonicalTestModel, TestStep


class CLIRuleGenerator:
    """Generates CLI validation rules from a CanonicalTestModel."""

    @classmethod
    def generate(
        cls,
        model: CanonicalTestModel,
        output_dir: str,
    ) -> str:
        """
        Generate a JSON file containing CLI validation rules.

        Args:
            model:      The canonical test model.
            output_dir: Directory to write the JSON file.

        Returns:
            Absolute path to the generated JSON file.
        """
        os.makedirs(output_dir, exist_ok=True)
        safe_title = _safe_filename(model.document_title)
        output_path = os.path.join(output_dir, f"{safe_title}_cli_rules.json")

        rules = {
            "document_title": model.document_title,
            "source_file": os.path.basename(model.source_file),
            "mop_structure": model.mop_structure,
            "total_steps": len(model.steps),
            "rules": cls._extract_rules(model),
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(rules, f, indent=2, ensure_ascii=False)

        return output_path

    @classmethod
    def _extract_rules(cls, model: CanonicalTestModel) -> List[Dict[str, Any]]:
        """Extract validation rules from all steps."""
        all_rules = []
        for step in model.steps:
            step_rules = cls._step_to_rules(step)
            all_rules.extend(step_rules)
        return all_rules

    @classmethod
    def _step_to_rules(cls, step: TestStep) -> List[Dict[str, Any]]:
        """Convert a TestStep to a list of CLI rules."""
        rules = []

        for cmd in step.commands:
            if not cmd.raw.strip():
                continue

            rule: Dict[str, Any] = {
                "step_sequence": step.sequence,
                "step_id": step.step_id,
                "step_type": step.step_type.value,
                "section": step.section,
                "command": cmd.raw,
                "vendor": cmd.vendor,
                "protocol": cmd.protocol,
                "mode": cmd.mode,
                "confidence": cmd.confidence,
                "is_rollback": step.is_rollback,
                "must_contain": cls._parse_must_contain(step.expected_output),
                "must_not_contain": ["Error", "Invalid input", "%"],
                "tags": step.tags,
                "description": step.description,
            }
            rules.append(rule)

        # If step has no commands but is a verification step, create a note rule
        if not step.commands and step.step_type.value == "verification":
            rules.append({
                "step_sequence": step.sequence,
                "step_id": step.step_id,
                "step_type": "manual_verification",
                "section": step.section,
                "command": None,
                "vendor": None,
                "protocol": None,
                "mode": None,
                "confidence": 1.0,
                "is_rollback": step.is_rollback,
                "must_contain": cls._parse_must_contain(step.expected_output),
                "must_not_contain": [],
                "tags": step.tags,
                "description": step.description,
            })

        return rules

    @classmethod
    def _parse_must_contain(cls, expected_output: Optional[str]) -> List[str]:
        """
        Extract keywords from expected_output that should appear in command output.

        Heuristic: extract quoted strings and state indicators.
        """
        if not expected_output:
            return []

        must_contain = []

        # Extract quoted strings
        quoted = re.findall(r"['\"]([^'\"]{2,})['\"]", expected_output)
        must_contain.extend(quoted)

        # Common state keywords
        state_keywords = [
            "Established", "Up", "Active", "Connected",
            "enabled", "ACTIVE", "FULL", "2-Way",
        ]
        for kw in state_keywords:
            if kw.lower() in expected_output.lower():
                must_contain.append(kw)

        return list(dict.fromkeys(must_contain))  # deduplicate, preserve order


def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w\-]", "_", name).strip("_")
