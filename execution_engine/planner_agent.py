"""
Planner Agent — builds an ExecutionPlan from a CanonicalTestModel.

Responsibilities:
  - Validate the dependency graph (no cycles, no unknown step references)
  - Compute execution waves (parallel groups via Kahn's algorithm)
  - Identify transaction groups for atomic rollback
  - Calculate the critical path (minimum sequential steps)
  - Determine whether approval is required before execution starts
  - Estimate total execution duration (lower bound)

Usage:
    from execution_engine.planner_agent import PlannerAgent
    plan = PlannerAgent().plan(canonical_model, execution_id)
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, List, Optional

from models.canonical import CanonicalTestModel, ExecutionStatus, FailureStrategy
from execution_engine.dag_engine import DAGEngine, DAGValidationError
from execution_engine.models import ExecutionPlan
from execution_engine.state_manager import state_manager

logger = logging.getLogger(__name__)


class PlannerAgent:
    """
    Converts a CanonicalTestModel into an ExecutionPlan.

    The plan is stored back into the ExecutionState metadata so that the
    ExecutionAgent can retrieve it without re-computing.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def plan(
        self,
        canonical_model: CanonicalTestModel,
        execution_id: str,
    ) -> ExecutionPlan:
        """
        Build and validate an ExecutionPlan for the given model.

        Args:
            canonical_model: The MOP model produced by Phase 1.
            execution_id:    Pre-created execution_id from state_manager.create().

        Returns:
            ExecutionPlan ready for ExecutionAgent.

        Raises:
            DAGValidationError: If the dependency graph is invalid.
        """
        logger.info("[%s] PlannerAgent starting", execution_id)

        steps = canonical_model.steps
        if not steps:
            raise ValueError("Cannot plan execution: CanonicalTestModel has no steps")

        # 1. Build and validate DAG
        dag = DAGEngine(steps)
        try:
            dag.validate()
        except DAGValidationError as exc:
            logger.error("[%s] DAG validation failed: %s", execution_id, exc)
            raise

        # 2. Execution waves
        waves = dag.waves()
        logger.info("[%s] DAG produces %d wave(s) for %d step(s)",
                    execution_id, len(waves), len(steps))

        # 3. Critical path
        critical_path = dag.critical_path()
        logger.debug("[%s] Critical path (%d steps): %s",
                     execution_id, len(critical_path), critical_path)

        # 4. Transaction groups
        transaction_groups = self._build_transaction_groups(steps)

        # 5. Device list
        device_list = self._collect_devices(canonical_model)

        # 6. Approval check
        requires_approval, approval_reasons = self._check_approval(canonical_model)

        # 7. Estimated duration (lower bound along critical path)
        estimated_duration_s = dag.estimated_duration_s()

        plan = ExecutionPlan(
            execution_id=execution_id,
            waves=waves,
            transaction_groups=transaction_groups,
            critical_path=critical_path,
            requires_approval=requires_approval,
            approval_reasons=approval_reasons,
            device_list=device_list,
            estimated_duration_s=estimated_duration_s,
        )

        # Persist approval status into execution state
        from models.canonical import ApprovalStatus
        state_manager.update_field(
            execution_id,
            approval_status=(
                ApprovalStatus.PENDING if requires_approval else ApprovalStatus.NOT_REQUIRED
            ),
        )

        # Check for blocked commands
        blocked_violations = self._check_blocked_commands(canonical_model, execution_id)
        if blocked_violations:
            raise ValueError(
                f"Plan rejected: {len(blocked_violations)} blocked command(s) found:\n"
                + "\n".join(blocked_violations)
            )

        self._log_plan_summary(plan, canonical_model)
        return plan

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_transaction_groups(
        self, steps
    ) -> Dict[str, List[str]]:
        """
        Group step_ids by their transaction_group label.

        Only groups with ≥2 steps are included (single-step groups are trivial).
        """
        groups: Dict[str, List[str]] = defaultdict(list)
        for step in steps:
            if step.transaction_group:
                groups[step.transaction_group].append(step.step_id)
        return {k: v for k, v in groups.items() if len(v) >= 2}

    def _collect_devices(self, canonical_model: CanonicalTestModel) -> List[str]:
        """Return unique, sorted list of all target hostnames in the model."""
        seen: set = set()
        for step in canonical_model.steps:
            for d in step.devices:
                if d.hostname:
                    seen.add(d.hostname)
        return sorted(seen)

    def _check_approval(
        self, canonical_model: CanonicalTestModel
    ) -> tuple[bool, List[str]]:
        """
        Determine whether human approval is required before execution.

        Approval is required if:
          - canonical_model.approval_required is True, OR
          - any step has approval_required=True, OR
          - any step has blast_radius=HIGH or CRITICAL
        """
        from models.canonical import BlastRadius

        reasons: List[str] = []

        if canonical_model.approval_required:
            reasons.append("Model-level approval_required flag is set")

        high_blast = [
            s.step_id for s in canonical_model.steps
            if s.blast_radius in (BlastRadius.HIGH, BlastRadius.CRITICAL)
        ]
        if high_blast:
            reasons.append(
                f"{len(high_blast)} step(s) have HIGH/CRITICAL blast radius: "
                + ", ".join(high_blast[:5])
            )

        per_step_approvals = [s.step_id for s in canonical_model.steps if s.approval_required]
        if per_step_approvals:
            reasons.append(
                f"{len(per_step_approvals)} step(s) require individual approval: "
                + ", ".join(per_step_approvals[:5])
            )

        return bool(reasons), reasons

    def _check_blocked_commands(
        self,
        canonical_model: CanonicalTestModel,
        execution_id: str,
    ) -> List[str]:
        """
        Return list of violation messages for any commands matching the blocked list.
        Raises ValueError if violations found (normalization rejection).
        """
        import re
        try:
            import yaml
            from pathlib import Path
            cfg_path = Path("configs") / "execution_defaults.yaml"
            if not cfg_path.exists():
                return []
            with open(cfg_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            blocked_patterns = cfg.get("blocked_commands", [])
        except Exception:
            return []

        if not blocked_patterns:
            return []

        violations = []
        for step in canonical_model.steps:
            for cmd in step.commands:
                raw = (cmd.normalized or cmd.raw or "").strip()
                for pattern in blocked_patterns:
                    try:
                        if re.search(pattern, raw, re.IGNORECASE):
                            # HIGH/CRITICAL blast radius with approval can override
                            from models.canonical import BlastRadius
                            if step.blast_radius in (BlastRadius.HIGH, BlastRadius.CRITICAL) and step.approval_required:
                                logger.warning(
                                    "[%s] Blocked command '%s' in step %s — allowed due to approval_required+blast_radius=%s",
                                    execution_id, raw[:60], step.step_id, step.blast_radius.value
                                )
                            else:
                                violations.append(
                                    f"Step {step.step_id}: command '{raw[:60]}' matches blocked pattern '{pattern}'"
                                )
                    except re.error:
                        pass
        return violations

    def _log_plan_summary(
        self, plan: ExecutionPlan, canonical_model: CanonicalTestModel
    ) -> None:
        logger.info(
            "[%s] Plan complete: %d waves, %d steps, %d transaction groups, "
            "%d devices, est. %.0fs%s",
            plan.execution_id,
            len(plan.waves),
            sum(len(w) for w in plan.waves),
            len(plan.transaction_groups),
            len(plan.device_list),
            plan.estimated_duration_s,
            " — REQUIRES APPROVAL" if plan.requires_approval else "",
        )
        for i, wave in enumerate(plan.waves):
            logger.debug("[%s]   Wave %d: %s", plan.execution_id, i, wave)
        # Additionally produce dry-run ASCII summary if applicable
        try:
            state = state_manager.get(plan.execution_id)
            if state.dry_run:
                self._print_dry_run_summary(plan, canonical_model)
        except Exception:
            pass

    def _print_dry_run_summary(self, plan: ExecutionPlan, canonical_model: CanonicalTestModel) -> None:
        """Print a human-readable DAG / execution plan for dry-run mode."""
        from models.canonical import BlastRadius
        lines = [
            "",
            "=" * 70,
            f"  DRY RUN EXECUTION PLAN — {canonical_model.document_title}",
            f"  Execution ID : {plan.execution_id}",
            f"  Steps        : {sum(len(w) for w in plan.waves)}",
            f"  Waves        : {len(plan.waves)}",
            f"  Devices      : {', '.join(plan.device_list) or '(none specified)'}",
            f"  Est. Duration: {plan.estimated_duration_s:.0f}s",
            f"  Approval     : {'REQUIRED' if plan.requires_approval else 'not required'}",
            "=" * 70,
        ]
        step_index = {s.step_id: s for s in canonical_model.steps}
        for i, wave in enumerate(plan.waves):
            lines.append(f"\n  Wave {i+1} ({len(wave)} step(s) in parallel):")
            for sid in wave:
                step = step_index.get(sid)
                if not step:
                    continue
                cmds = ", ".join(c.raw[:40] for c in step.commands[:2])
                if len(step.commands) > 2:
                    cmds += f" (+{len(step.commands)-2} more)"
                devices_str = ", ".join(d.hostname for d in step.devices) or "—"
                blast_flag = " ⚠ CRITICAL" if step.blast_radius == BlastRadius.CRITICAL else (
                             " ⚠ HIGH" if step.blast_radius.value in ("high",) else "")
                approval_flag = " [APPROVAL REQUIRED]" if step.approval_required else ""
                lines.append(
                    f"    [{sid}] {step.description[:50]}{blast_flag}{approval_flag}"
                )
                lines.append(f"         devices={devices_str}  cmds={cmds}")
            if i < len(plan.waves) - 1:
                lines.append("         ↓ (wait for wave completion)")
        if plan.transaction_groups:
            lines.append("\n  Transaction Groups:")
            for grp, ids in plan.transaction_groups.items():
                lines.append(f"    {grp}: {ids}")
        if plan.critical_path:
            lines.append(f"\n  Critical Path: {' → '.join(plan.critical_path)}")
        lines.append("=" * 70)
        for line in lines:
            logger.info(line)
