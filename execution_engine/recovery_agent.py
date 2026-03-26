"""
Recovery Agent — handles failures, rollbacks, and retry decisions.

Responsibilities:
  1. Decide the recovery action for a failed step (RETRY / ROLLBACK / CONTINUE /
     ESCALATE) based on the model's FailureStrategy and step configuration.
  2. Execute rollback_commands for individual steps (using is_rollback=True steps).
  3. Rollback an entire transaction group (reverse topological order).
  4. Rollback all steps executed so far (full backout).

RecoveryAgent is called by ExecutionAgent — it never drives execution
directly and always goes through state_manager for state changes.

Usage:
    from execution_engine.recovery_agent import RecoveryAgent
    recovery = RecoveryAgent()
    recovery.rollback_group("group_name", group_step_ids, model, execution_id, device_map)
    recovery.rollback_all(execution_id, model, device_map)
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

from models.canonical import (
    CanonicalTestModel,
    ExecutionStatus,
    FailureStrategy,
    TestStep,
    ActionType,
)
from device_layer.device_driver import DeviceDriver, MockDriver, DeviceCommandError
from execution_engine.kill_switch import kill_switch
from execution_engine.state_manager import state_manager

logger = logging.getLogger(__name__)


class RecoveryAgent:
    """
    Handles all failure recovery scenarios for the Execution Agent.

    The agent is stateless between calls — all state comes from state_manager.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def rollback_step(
        self,
        step: TestStep,
        driver: Optional[DeviceDriver],
        execution_id: str = "",
    ) -> bool:
        """
        Execute the rollback procedure for a single step.

        Rollback commands are:
          1. Any step in the model with is_rollback=True that references this step's
             section (heuristic).
          2. For Cisco/Juniper: 'no' prepended versions of config commands.

        Returns True if rollback succeeded (or was a no-op), False on error.
        """
        tag = f"[{execution_id}:{step.step_id}]"

        rollback_cmds = self._build_rollback_commands(step)
        if not rollback_cmds:
            logger.debug("%s No rollback commands — step has no reversible config", tag)
            return True

        if driver is None:
            logger.warning("%s Rollback skipped — no driver available", tag)
            return False

        logger.info("%s Executing %d rollback command(s)", tag, len(rollback_cmds))
        state_manager.transition_step(
            execution_id, step.step_id, ExecutionStatus.ROLLING_BACK,
            agent="recovery", message="Rollback started",
        )

        success = True
        for cmd in rollback_cmds:
            if kill_switch.is_set():
                logger.warning("%s Kill switch during rollback — stopping", tag)
                success = False
                break
            try:
                output = driver.execute(cmd, timeout_s=30.0)
                logger.debug("%s Rollback CMD: %s → %s", tag, cmd, output[:80])
            except DeviceCommandError as exc:
                logger.error("%s Rollback command failed: %s — %s", tag, cmd, exc)
                success = False

        final_status = ExecutionStatus.ROLLED_BACK if success else ExecutionStatus.FAILED
        state_manager.transition_step(
            execution_id, step.step_id, final_status,
            agent="recovery",
            message="Rollback completed" if success else "Rollback failed",
        )
        return success

    def rollback_group(
        self,
        group_name: str,
        group_step_ids: List[str],
        model: CanonicalTestModel,
        execution_id: str,
        device_map: Dict[str, DeviceDriver],
    ) -> bool:
        """
        Roll back all steps in a transaction group in reverse order.

        Only rolls back steps that have PASSED (i.e., were actually applied).
        Steps that never ran are skipped.

        Returns True if all applicable rollbacks succeeded.
        """
        logger.info("[%s] Rollback group '%s' (%d steps)",
                    execution_id, group_name, len(group_step_ids))

        step_index = {s.step_id: s for s in model.steps}
        state = state_manager.get(execution_id)

        # Find the most recent savepoint among PASSED steps (highest sequence number)
        savepoint_seq = -1
        for sid in group_step_ids:
            step = step_index.get(sid)
            result = state.steps.get(sid)
            if step and result and step.savepoint and result.status == ExecutionStatus.PASSED:
                if step.sequence > savepoint_seq:
                    savepoint_seq = step.sequence

        reversed_ids = list(reversed(group_step_ids))
        all_ok = True

        for step_id in reversed_ids:
            if kill_switch.is_set() or state_manager.is_killed(execution_id):
                logger.warning("[%s] Kill switch during group rollback", execution_id)
                break

            step = step_index.get(step_id)
            result = state.steps.get(step_id)
            if step is None or result is None or result.status != ExecutionStatus.PASSED:
                logger.debug("[%s:%s] Skipping rollback — step not in PASSED state",
                             execution_id, step_id)
                continue

            # Stop at savepoint — don't roll back steps at or before it
            if savepoint_seq >= 0 and step.sequence <= savepoint_seq:
                logger.info("[%s] Savepoint reached at seq=%d — stopping group rollback",
                            execution_id, savepoint_seq)
                break

            driver = self._get_driver(step, device_map, execution_id)
            ok = self.rollback_step(step, driver, execution_id)
            if not ok:
                all_ok = False

        return all_ok

    def rollback_all(
        self,
        execution_id: str,
        model: CanonicalTestModel,
        device_map: Dict[str, DeviceDriver],
    ) -> bool:
        """
        Roll back ALL steps that have PASSED, in reverse execution order.

        Used for FailureStrategy.ROLLBACK_ALL.
        """
        logger.info("[%s] Full rollback initiated", execution_id)

        state = state_manager.get(execution_id)
        step_index = {s.step_id: s for s in model.steps}

        # Sort passed steps by sequence (descending = reverse order)
        passed_steps = sorted(
            [
                step_index[sid]
                for sid, result in state.steps.items()
                if result.status == ExecutionStatus.PASSED and sid in step_index
            ],
            key=lambda s: s.sequence,
            reverse=True,
        )

        if not passed_steps:
            logger.info("[%s] No PASSED steps to roll back", execution_id)
            return True

        logger.info("[%s] Rolling back %d PASSED step(s)", execution_id, len(passed_steps))
        all_ok = True

        for step in passed_steps:
            if kill_switch.is_set() or state_manager.is_killed(execution_id):
                logger.warning("[%s] Kill switch during full rollback", execution_id)
                break
            driver = self._get_driver(step, device_map, execution_id)
            ok = self.rollback_step(step, driver, execution_id)
            if not ok:
                all_ok = False

        return all_ok

    def make_decision(
        self,
        step: TestStep,
        error_message: str,
        attempt: int,
        model: CanonicalTestModel,
        execution_id: str = "",
        evidence: str = "",
    ) -> str:
        """
        Determine the recovery decision for a failed step.

        Returns one of: RETRY | ROLLBACK | CONTINUE | ESCALATE | ABORT

        This is called by ExecutionAgent's retry loop before each retry attempt
        to allow policy-based overrides (e.g., certain error types always escalate).
        """
        strategy = model.failure_strategy or FailureStrategy.ABORT
        max_retries = step.execution_policy.retry_count
        reason = ""
        confidence = 0.7

        # Always retry if within limit
        if attempt < max_retries:
            decision = "RETRY"
            reason = f"attempt {attempt} < max_retries {max_retries}"
            confidence = 1.0
        elif self._is_fatal_error(error_message):
            # Check for non-recoverable error patterns first
            logger.warning("[%s] Fatal error detected — escalating", step.step_id)
            decision = "ESCALATE"
            reason = f"Fatal error pattern detected in: {error_message[:80]}"
            confidence = 1.0
        elif strategy == FailureStrategy.CONTINUE:
            decision = "CONTINUE"
            reason = "FailureStrategy=CONTINUE"
            confidence = 0.7
        elif strategy in (FailureStrategy.ROLLBACK_GROUP, FailureStrategy.ROLLBACK_ALL):
            decision = "ROLLBACK"
            reason = f"FailureStrategy={strategy.value}"
            confidence = 0.7
        else:
            decision = "ABORT"
            reason = "Max retries exhausted, no recovery strategy"
            confidence = 0.5

        self._write_decision_record(
            step=step,
            decision=decision,
            reason=reason,
            evidence=evidence or error_message,
            confidence=confidence,
            execution_id=execution_id,
        )

        if confidence < 0.6:
            logger.warning(
                "[%s] Decision '%s' has low confidence (%.2f) — should be reviewed",
                step.step_id, decision, confidence,
            )

        return decision

    def _write_decision_record(
        self,
        step: TestStep,
        decision: str,
        reason: str,
        evidence: str,
        confidence: float,
        execution_id: str = "",
    ) -> None:
        """Append a JSON decision record to output/decision.log (one record per line)."""
        from datetime import datetime, timezone
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "execution_id": execution_id,
            "step_id": step.step_id,
            "decision": decision,
            "reason": reason,
            "evidence": evidence[:200],
            "confidence": confidence,
            "auto_decided": confidence >= 0.6,
            "correlation_id": f"{execution_id}:{step.step_id}",
        }
        log_path = Path("output") / "decision.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_rollback_commands(self, step: TestStep) -> List[str]:
        """
        Build the list of rollback commands for a step.

        Strategy (in priority order):
          1. If any command is a config-mode command, prepend 'no ' (Cisco/Arista/Nokia)
          2. Juniper: generate 'delete/deactivate' equivalents for 'set/activate'
          3. If step.action_type is ROLLBACK — nothing to rollback (this IS rollback)
          4. Verify/observe steps — no rollback needed
        """
        if step.action_type in (ActionType.VERIFY, ActionType.OBSERVE, ActionType.ROLLBACK):
            return []

        if not step.commands:
            return []

        rollback_cmds: List[str] = []
        for cmd in reversed(step.commands):  # reverse config order
            raw = cmd.raw.strip()
            vendor = (cmd.vendor or "generic").lower()

            # Skip exec-mode read-only commands
            if cmd.mode == "exec":
                continue

            if vendor in ("cisco", "arista", "nokia", "ericsson"):
                if not raw.startswith("no ") and not raw.startswith("end"):
                    rollback_cmds.append(f"no {raw}")
            elif vendor == "juniper":
                if raw.startswith("set "):
                    rollback_cmds.append(raw.replace("set ", "delete ", 1))
                elif raw.startswith("activate "):
                    rollback_cmds.append(raw.replace("activate ", "deactivate ", 1))
            elif vendor == "huawei":
                if not raw.startswith("undo "):
                    rollback_cmds.append(f"undo {raw}")
            else:
                # Generic: attempt 'no' prefix
                if not raw.startswith("no "):
                    rollback_cmds.append(f"no {raw}")

        return rollback_cmds

    def _get_driver(
        self,
        step: TestStep,
        device_map: Dict[str, DeviceDriver],
        execution_id: str,
    ) -> Optional[DeviceDriver]:
        """Get driver for step's primary device (mirror of ExecutionAgent logic)."""
        if not step.devices:
            return None

        hostname = step.devices[0].hostname
        if hostname in device_map:
            drv = device_map[hostname]
            if not drv.is_connected:
                try:
                    drv.connect()
                except Exception as exc:
                    logger.error("[%s] Cannot reconnect to %s: %s",
                                 execution_id, hostname, exc)
                    return None
            return drv

        # Try connection pool
        try:
            from device_layer.connection_pool import connection_pool
            vendor = (step.commands[0].vendor if step.commands else "generic") or "generic"
            return connection_pool.acquire(hostname, vendor=vendor)
        except Exception as exc:
            logger.error("[%s] Cannot acquire driver for %s: %s",
                         execution_id, hostname, exc)
            return None

    def _is_fatal_error(self, error_message: str) -> bool:
        """Heuristic: certain error strings should never be retried."""
        fatal_patterns = [
            "permission denied",
            "authentication failed",
            "no route to host",
            "connection refused",
            "host key verification failed",
        ]
        lower = error_message.lower()
        return any(p in lower for p in fatal_patterns)
