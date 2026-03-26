"""
MOP Quality Scorer

Analyses a CanonicalTestModel and assigns a quality score BEFORE execution,
so engineers know how much they can trust the automated output.

Scoring (max 12 points):
  Commands detected      0 cmds=0 | 1–5=1 | 6–15=2 | 16+=3
  Rollback steps present yes=2    | no=0
  Pre-checks present     yes=1    | no=0
  Verification present   yes=1    | no=0
  Expected-output cover  >50%=2   | >20%=1 | else=0
  Avg command confidence >0.85=1  | else=0
  Section diversity      ≥3=1     | else=0
  Failure strategy set   non-abort=1 | else=0

Bands:
  HIGH   ≥ 8  — safe to automate, minor manual review recommended
  MEDIUM 4–7  — automate with caution, review before production
  LOW    0–3  — significant gaps, manual review required before use
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from models.canonical import CanonicalTestModel, FailureStrategy


@dataclass
class QualityScore:
    score: int
    max_score: int
    band: str                    # HIGH | MEDIUM | LOW
    breakdown: dict              # component → points earned
    warnings: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)

    @property
    def percentage(self) -> int:
        return round(self.score / self.max_score * 100)

    def summary_line(self) -> str:
        bar_filled = round(self.percentage / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        return (
            f"Quality: {self.band} [{bar}] {self.score}/{self.max_score} ({self.percentage}%)"
        )


class QualityScorer:

    MAX_SCORE = 12

    @classmethod
    def score(cls, model: CanonicalTestModel) -> QualityScore:
        breakdown: dict = {}
        warnings: List[str] = []
        recommendations: List[str] = []
        total = 0

        steps = model.steps
        non_rollback = [s for s in steps if not s.is_rollback]
        rollback_steps = [s for s in steps if s.is_rollback]
        sections = {s.section for s in steps if s.section}

        # --- Commands detected ---
        cmd_count = sum(len(s.commands) for s in steps)
        if cmd_count == 0:
            pts = 0
            warnings.append("No CLI commands detected — document may be prose-only or unrecognised format")
            recommendations.append("Verify the PDF contains actual CLI commands, not just descriptions")
        elif cmd_count <= 5:
            pts = 1
            warnings.append(f"Only {cmd_count} CLI commands detected — MOP may be incomplete")
        elif cmd_count <= 15:
            pts = 2
        else:
            pts = 3
        breakdown["commands_detected"] = {"points": pts, "max": 3, "detail": f"{cmd_count} commands"}
        total += pts

        # --- Rollback steps ---
        if rollback_steps:
            pts = 2
        else:
            pts = 0
            warnings.append("No rollback steps found — cannot auto-rollback on failure")
            recommendations.append("Add a Rollback section to the MOP with 'no <cmd>' or 'undo <cmd>' reversal steps")
        breakdown["rollback_steps"] = {"points": pts, "max": 2, "detail": f"{len(rollback_steps)} rollback steps"}
        total += pts

        # --- Pre-checks section ---
        has_pre = any((s.section or "").lower() in ("pre-checks", "pre checks", "pre_checks") for s in steps)
        pts = 1 if has_pre else 0
        if not has_pre:
            recommendations.append("Add a Pre-checks section to capture baseline state before changes")
        breakdown["pre_checks"] = {"points": pts, "max": 1, "detail": "present" if has_pre else "missing"}
        total += pts

        # --- Verification section ---
        has_verify = any(
            (s.section or "").lower() in ("verification", "post-checks", "post checks")
            for s in steps
        )
        pts = 1 if has_verify else 0
        if not has_verify:
            recommendations.append("Add a Verification section to confirm changes took effect")
        breakdown["verification_section"] = {"points": pts, "max": 1, "detail": "present" if has_verify else "missing"}
        total += pts

        # --- Expected output coverage ---
        steps_with_expected = sum(1 for s in non_rollback if s.expected_output)
        coverage = steps_with_expected / len(non_rollback) if non_rollback else 0
        if coverage > 0.5:
            pts = 2
        elif coverage > 0.2:
            pts = 1
            recommendations.append(
                f"Only {steps_with_expected}/{len(non_rollback)} steps have expected outputs — "
                "add success criteria to improve validation"
            )
        else:
            pts = 0
            warnings.append(
                f"Only {steps_with_expected}/{len(non_rollback)} steps have expected outputs — "
                "validation will be limited to error-pattern matching only"
            )
            recommendations.append(
                "Define expected outputs (e.g. 'BGP neighbors in Established state') for verification steps"
            )
        breakdown["expected_output_coverage"] = {
            "points": pts,
            "max": 2,
            "detail": f"{steps_with_expected}/{len(non_rollback)} steps ({round(coverage*100)}%)",
        }
        total += pts

        # --- Average command confidence ---
        all_cmds = [cmd for s in steps for cmd in s.commands]
        avg_conf = sum(c.confidence for c in all_cmds) / len(all_cmds) if all_cmds else 0
        pts = 1 if avg_conf >= 0.85 else 0
        if avg_conf < 0.85:
            recommendations.append(
                f"Average command confidence is {avg_conf:.0%} — some commands may be misidentified"
            )
        breakdown["command_confidence"] = {
            "points": pts,
            "max": 1,
            "detail": f"{avg_conf:.0%} avg confidence",
        }
        total += pts

        # --- Section diversity ---
        pts = 1 if len(sections) >= 3 else 0
        if len(sections) < 3:
            recommendations.append(
                f"Only {len(sections)} section(s) detected — well-structured MOPs have at least "
                "Pre-checks, Implementation, Verification, and Rollback"
            )
        breakdown["section_diversity"] = {
            "points": pts,
            "max": 1,
            "detail": f"{len(sections)} sections: {', '.join(sorted(sections)) or 'none'}",
        }
        total += pts

        # --- Failure strategy ---
        strategy = model.failure_strategy or FailureStrategy.ABORT
        pts = 1 if strategy != FailureStrategy.ABORT else 0
        if strategy == FailureStrategy.ABORT:
            recommendations.append(
                "failure_strategy=ABORT — consider ROLLBACK_ALL for production MOPs to auto-revert on failure"
            )
        breakdown["failure_strategy"] = {
            "points": pts,
            "max": 1,
            "detail": strategy.value,
        }
        total += pts

        # --- Band ---
        if total >= 8:
            band = "HIGH"
        elif total >= 4:
            band = "MEDIUM"
        else:
            band = "LOW"

        return QualityScore(
            score=total,
            max_score=cls.MAX_SCORE,
            band=band,
            breakdown=breakdown,
            warnings=warnings,
            recommendations=recommendations,
        )

    @classmethod
    def print_report(cls, qs: QualityScore) -> None:
        """Print a formatted quality report to stdout."""
        band_colours = {"HIGH": "✅", "MEDIUM": "⚠️ ", "LOW": "❌"}
        icon = band_colours.get(qs.band, "")
        print()
        print("=" * 62)
        print(f"  MOP QUALITY REPORT  {icon} {qs.band}")
        print("=" * 62)
        print(f"  {qs.summary_line()}")
        print()
        print("  Breakdown:")
        for component, info in qs.breakdown.items():
            label = component.replace("_", " ").title()
            bar = "▓" * info["points"] + "░" * (info["max"] - info["points"])
            print(f"    {label:<28} [{bar}] {info['points']}/{info['max']}  {info['detail']}")
        if qs.warnings:
            print()
            print("  Warnings:")
            for w in qs.warnings:
                print(f"    ⚠  {w}")
        if qs.recommendations:
            print()
            print("  Recommendations:")
            for r in qs.recommendations:
                print(f"    →  {r}")
        print("=" * 62)
        print()
