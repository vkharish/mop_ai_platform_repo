"""
Post-Processing Guardrails

Validates the CanonicalTestModel after LLM extraction by cross-checking
against grammar engine results and enforcing quality rules.

Two checkpoints:
  1. PRE-LLM:  count grammar-detected commands (stored as baseline)
  2. POST-LLM: compare LLM-extracted commands against baseline

Additional quality checks:
  - Every step has a description
  - No duplicate step sequences
  - Rollback steps marked correctly
  - CLI commands are non-empty
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from models.canonical import CanonicalTestModel, TestStep

logger = logging.getLogger(__name__)


@dataclass
class GuardrailResult:
    """Result of the guardrail validation pass."""

    passed: bool
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    pre_llm_command_count: int = 0
    post_llm_command_count: int = 0
    coverage_ratio: Optional[float] = None

    @property
    def summary(self) -> str:
        status = "PASSED" if self.passed else "FAILED"
        lines = [f"Guardrail result: {status}"]
        lines.append(f"  Commands: pre-LLM={self.pre_llm_command_count}, "
                     f"post-LLM={self.post_llm_command_count}, "
                     f"coverage={self.coverage_ratio:.0%}" if self.coverage_ratio is not None
                     else f"  Commands: pre-LLM={self.pre_llm_command_count}, "
                     f"post-LLM={self.post_llm_command_count}")
        for w in self.warnings:
            lines.append(f"  WARNING: {w}")
        for e in self.errors:
            lines.append(f"  ERROR:   {e}")
        return "\n".join(lines)


class Guardrails:
    """
    Runs pre-LLM and post-LLM quality checks on the pipeline output.
    """

    # If LLM recovers fewer than this fraction of grammar-detected commands,
    # raise a warning (not an error — LLM may have found more via context)
    COMMAND_COVERAGE_WARN_THRESHOLD = 0.5

    # If a step description is shorter than this, flag it
    MIN_DESCRIPTION_LENGTH = 5

    @classmethod
    def validate(
        cls,
        model: CanonicalTestModel,
        pre_llm_command_count: int = 0,
    ) -> GuardrailResult:
        """
        Run all guardrail checks on the canonical model.

        Args:
            model:                   The CanonicalTestModel from the LLM.
            pre_llm_command_count:   Commands detected by grammar engine pre-LLM.

        Returns:
            GuardrailResult with pass/fail, warnings, and errors.
        """
        result = GuardrailResult(passed=True)
        result.pre_llm_command_count = pre_llm_command_count

        cls._check_has_steps(model, result)
        cls._check_step_descriptions(model.steps, result)
        cls._check_no_duplicate_sequences(model.steps, result)
        cls._check_empty_commands(model.steps, result)
        cls._check_rollback_consistency(model.steps, result)
        cls._check_command_coverage(model, pre_llm_command_count, result)

        if result.errors:
            result.passed = False

        logger.info(result.summary)
        return result

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    @classmethod
    def _check_has_steps(cls, model: CanonicalTestModel, result: GuardrailResult):
        if not model.steps:
            result.errors.append(
                "No steps were extracted from the document. "
                "The LLM may have returned an empty steps array."
            )

    @classmethod
    def _check_step_descriptions(cls, steps: List[TestStep], result: GuardrailResult):
        for step in steps:
            if not step.description or len(step.description.strip()) < cls.MIN_DESCRIPTION_LENGTH:
                result.warnings.append(
                    f"Step {step.sequence} has a very short or missing description: "
                    f"'{step.description}'"
                )

    @classmethod
    def _check_no_duplicate_sequences(cls, steps: List[TestStep], result: GuardrailResult):
        seen = {}
        for step in steps:
            if step.sequence in seen:
                result.warnings.append(
                    f"Duplicate sequence number {step.sequence} found "
                    f"(step_ids: {seen[step.sequence]}, {step.step_id})"
                )
            seen[step.sequence] = step.step_id

    @classmethod
    def _check_empty_commands(cls, steps: List[TestStep], result: GuardrailResult):
        for step in steps:
            for cmd in step.commands:
                if not cmd.raw or not cmd.raw.strip():
                    result.warnings.append(
                        f"Step {step.sequence} has a command with empty 'raw' field."
                    )

    @classmethod
    def _check_rollback_consistency(cls, steps: List[TestStep], result: GuardrailResult):
        """Check that rollback step_type and is_rollback flag are consistent."""
        for step in steps:
            if step.step_type.value == "rollback" and not step.is_rollback:
                result.warnings.append(
                    f"Step {step.sequence} has step_type='rollback' "
                    f"but is_rollback=False. Correcting is_rollback to True."
                )
                step.is_rollback = True  # auto-correct

            if step.is_rollback and step.step_type.value not in ("rollback", "action", "verification"):
                result.warnings.append(
                    f"Step {step.sequence} is marked is_rollback=True "
                    f"but step_type='{step.step_type}'. Consider using step_type='rollback'."
                )

    @classmethod
    def _check_command_coverage(
        cls,
        model: CanonicalTestModel,
        pre_llm_count: int,
        result: GuardrailResult,
    ):
        """
        Compare grammar-engine command count vs LLM-extracted command count.

        The LLM may find MORE commands than the grammar engine (prose MOPs,
        inline commands), so we only warn if coverage is very low.
        """
        post_llm_count = sum(len(s.commands) for s in model.steps)
        result.post_llm_command_count = post_llm_count

        if pre_llm_count == 0:
            # Grammar engine found nothing (prose MOP or no CLI doc) — OK
            return

        ratio = post_llm_count / pre_llm_count
        result.coverage_ratio = ratio

        if ratio < cls.COMMAND_COVERAGE_WARN_THRESHOLD:
            result.warnings.append(
                f"Low command coverage: grammar engine detected {pre_llm_count} commands, "
                f"but LLM only extracted {post_llm_count} ({ratio:.0%}). "
                "Some CLI commands may have been missed."
            )
