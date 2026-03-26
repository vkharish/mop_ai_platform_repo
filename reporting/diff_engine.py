"""
Pre/Post Diff Engine

Compares pre-check baseline outputs with post-change verification outputs
to surface exactly what changed on the device after a MOP was executed.

Two modes:
  1. Text diff   — compare two raw CLI output strings (used by execution engine
                   and Robot Framework keyword)
  2. Step diff   — compare two canonical JSON files / models to show what
                   steps were added, removed, or changed between MOP versions

Usage:
    from reporting.diff_engine import DiffEngine

    result = DiffEngine.diff_text(baseline, current, label="show bgp summary")
    print(result.summary())

    step_diff = DiffEngine.diff_steps(model_v1, model_v2)
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from models.canonical import CanonicalTestModel


# ---------------------------------------------------------------------------
# Text diff result
# ---------------------------------------------------------------------------

@dataclass
class TextDiffResult:
    label: str
    baseline: str
    current: str
    added_lines: List[str] = field(default_factory=list)
    removed_lines: List[str] = field(default_factory=list)
    changed: bool = False
    diff_lines: List[str] = field(default_factory=list)

    @property
    def is_identical(self) -> bool:
        return not self.changed

    def summary(self) -> str:
        if self.is_identical:
            return f"[DIFF] {self.label}: NO CHANGE (outputs identical)"
        lines = [
            f"[DIFF] {self.label}: CHANGED",
            f"       Added   ({len(self.added_lines)} lines): "
            + (", ".join(self.added_lines[:3]) + ("…" if len(self.added_lines) > 3 else "") or "(none)"),
            f"       Removed ({len(self.removed_lines)} lines): "
            + (", ".join(self.removed_lines[:3]) + ("…" if len(self.removed_lines) > 3 else "") or "(none)"),
        ]
        return "\n".join(lines)

    def unified_diff(self) -> str:
        """Return unified diff string for embedding in reports."""
        return "\n".join(self.diff_lines)


# ---------------------------------------------------------------------------
# Step diff result
# ---------------------------------------------------------------------------

@dataclass
class StepDiffResult:
    added_steps: List[dict] = field(default_factory=list)
    removed_steps: List[dict] = field(default_factory=list)
    changed_steps: List[dict] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added_steps or self.removed_steps or self.changed_steps)

    def summary(self) -> str:
        if not self.has_changes:
            return "MOP DIFF: No changes between versions"
        parts = []
        if self.added_steps:
            parts.append(f"+{len(self.added_steps)} step(s) added")
        if self.removed_steps:
            parts.append(f"-{len(self.removed_steps)} step(s) removed")
        if self.changed_steps:
            parts.append(f"~{len(self.changed_steps)} step(s) modified")
        return "MOP DIFF: " + ", ".join(parts)


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------

class DiffEngine:

    @staticmethod
    def diff_text(
        baseline: str,
        current: str,
        label: str = "output",
        ignore_timestamps: bool = True,
        ignore_counters: bool = True,
    ) -> TextDiffResult:
        """
        Compare two CLI output strings (pre-check vs post-check).

        Args:
            baseline:          Pre-change output captured during pre-checks.
            current:           Post-change output captured during verification.
            label:             Human-readable label (e.g. the CLI command used).
            ignore_timestamps: Strip timestamp patterns before comparing.
            ignore_counters:   Strip packet/byte counter lines before comparing.

        Returns:
            TextDiffResult with per-line breakdown.
        """
        b_clean = DiffEngine._normalise(baseline, ignore_timestamps, ignore_counters)
        c_clean = DiffEngine._normalise(current, ignore_timestamps, ignore_counters)

        b_lines = b_clean.splitlines()
        c_lines = c_clean.splitlines()

        diff = list(difflib.unified_diff(
            b_lines, c_lines,
            fromfile="pre-check (baseline)",
            tofile="post-check (current)",
            lineterm="",
        ))

        added = [l[1:].strip() for l in diff if l.startswith("+") and not l.startswith("+++")]
        removed = [l[1:].strip() for l in diff if l.startswith("-") and not l.startswith("---")]

        return TextDiffResult(
            label=label,
            baseline=baseline,
            current=current,
            added_lines=[l for l in added if l],
            removed_lines=[l for l in removed if l],
            changed=bool(added or removed),
            diff_lines=diff,
        )

    @staticmethod
    def diff_steps(
        model_before: CanonicalTestModel,
        model_after: CanonicalTestModel,
    ) -> StepDiffResult:
        """
        Compare two CanonicalTestModel instances (e.g. v1 vs v2 of a MOP).
        Matches steps by sequence number and compares description + commands.
        """
        before_map: Dict[int, object] = {s.sequence: s for s in model_before.steps}
        after_map:  Dict[int, object] = {s.sequence: s for s in model_after.steps}

        all_seqs = sorted(set(before_map) | set(after_map))
        result = StepDiffResult()

        for seq in all_seqs:
            b = before_map.get(seq)
            a = after_map.get(seq)

            if b is None:
                result.added_steps.append({
                    "sequence": seq,
                    "description": a.description,
                    "section": a.section,
                })
            elif a is None:
                result.removed_steps.append({
                    "sequence": seq,
                    "description": b.description,
                    "section": b.section,
                })
            else:
                changes = DiffEngine._step_changes(b, a)
                if changes:
                    result.changed_steps.append({
                        "sequence": seq,
                        "description": a.description,
                        "changes": changes,
                    })

        return result

    @staticmethod
    def build_comparison_report(
        comparisons: List[Tuple[str, TextDiffResult]],
    ) -> str:
        """
        Build a human-readable pre/post comparison report from a list of
        (section_label, TextDiffResult) tuples.
        """
        lines = [
            "",
            "=" * 66,
            "  PRE / POST CHANGE COMPARISON REPORT",
            "=" * 66,
        ]
        changed_count = sum(1 for _, r in comparisons if r.changed)
        identical_count = len(comparisons) - changed_count

        lines.append(
            f"  Total checks: {len(comparisons)}  |  "
            f"Changed: {changed_count}  |  Identical: {identical_count}"
        )
        lines.append("=" * 66)

        for section, result in comparisons:
            if result.is_identical:
                lines.append(f"\n  ✅  {section}")
                lines.append("      Output unchanged — as expected")
            else:
                lines.append(f"\n  ⚠️   {section}  ← CHANGED")
                if result.added_lines:
                    lines.append(f"      + Added   : {'; '.join(result.added_lines[:5])}")
                if result.removed_lines:
                    lines.append(f"      - Removed : {'; '.join(result.removed_lines[:5])}")

        lines.append("\n" + "=" * 66)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise(text: str, strip_ts: bool, strip_counters: bool) -> str:
        """Normalise CLI output before diffing to reduce noise."""
        if strip_ts:
            # Remove common timestamp patterns: 12:34:56, Mar 25 2026, etc.
            text = re.sub(
                r"\b\d{1,2}:\d{2}:\d{2}(\.\d+)?\b"
                r"|\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
                r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
                r"\s+\d{1,2}\s+\d{4}\b"
                r"|\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\b",
                "<TS>",
                text,
            )
        if strip_counters:
            # Remove lines that are pure counter/uptime noise
            # e.g. "  5 packets transmitted" or "Up for 3d12h"
            text = re.sub(
                r"^\s*\d+\s+(packets?|bytes?|errors?|drops?|input|output)\b.*$",
                "<COUNTER>",
                text,
                flags=re.MULTILINE | re.IGNORECASE,
            )
        return text.strip()

    @staticmethod
    def _step_changes(before, after) -> List[str]:
        """Return list of human-readable change descriptions between two TestSteps."""
        changes = []
        if before.description != after.description:
            changes.append(f"description changed")
        b_cmds = {c.raw for c in before.commands}
        a_cmds = {c.raw for c in after.commands}
        added_cmds = a_cmds - b_cmds
        removed_cmds = b_cmds - a_cmds
        if added_cmds:
            changes.append(f"commands added: {', '.join(list(added_cmds)[:3])}")
        if removed_cmds:
            changes.append(f"commands removed: {', '.join(list(removed_cmds)[:3])}")
        if before.expected_output != after.expected_output:
            changes.append("expected_output changed")
        if before.section != after.section:
            changes.append(f"section moved: {before.section} → {after.section}")
        return changes
