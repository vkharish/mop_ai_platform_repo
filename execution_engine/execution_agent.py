"""
Execution Agent — core execution loop for Phase 2.

Orchestrates step-by-step execution of a CanonicalTestModel against real
(or mock) network devices using:
  - DAG-based parallel wave execution
  - Kill switch + pause/resume support
  - Per-step idempotency checks (skip-if-already-applied)
  - Smart wait / polling engine
  - Per-step validation after each command
  - Recovery agent delegation on failure

All state mutations go through state_manager — the agent never holds
its own copy of ExecutionState between operations.

Usage:
    from execution_engine.execution_agent import ExecutionAgent
    agent = ExecutionAgent(dry_run=True)
    result = agent.run(execution_id, device_map={"PE1": mock_driver})
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from typing import Dict, List, Optional

from models.canonical import (
    CanonicalTestModel,
    ExecutionStatus,
    FailureStrategy,
    TestStep,
    ActionType,
    ApprovalStatus,
)
from device_layer.device_driver import DeviceDriver, MockDriver, DeviceCommandError, DeviceConnectionError
from device_layer.connection_pool import connection_pool
from execution_engine.kill_switch import kill_switch
from execution_engine.models import ExecutionPlan, StepResult
from execution_engine.state_manager import state_manager
from execution_engine.planner_agent import PlannerAgent
from execution_engine.concurrency_controller import concurrency_controller
from execution_engine.validation_agent import ValidationAgent
import smart_wait.idempotency_engine as idempotency_engine_module
from smart_wait.polling_engine import PollingEngine

logger = logging.getLogger(__name__)

# Maximum parallel threads per wave (bounded by device count in practice)
_MAX_WAVE_WORKERS = 10

# Seconds to sleep between pause-check polls
_PAUSE_POLL_INTERVAL_S = 5.0


class ExecutionError(Exception):
    """Raised when execution fails and cannot be recovered."""


class ExecutionAgent:
    """
    Drives step-by-step MOP execution.

    Args:
        dry_run: If True, use MockDriver for all devices and skip real SSH.
        max_wave_workers: Max parallel threads within a single DAG wave.
    """

    def __init__(
        self,
        dry_run: bool = False,
        max_wave_workers: int = _MAX_WAVE_WORKERS,
    ) -> None:
        self._dry_run = dry_run
        self._max_workers = max_wave_workers
        self._validator = ValidationAgent()
        self._polling = PollingEngine()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        execution_id: str,
        device_map: Optional[Dict[str, DeviceDriver]] = None,
    ) -> ExecutionStatus:
        """
        Execute a previously created execution record.

        Args:
            execution_id: ID returned by state_manager.create().
            device_map:   Hostname → DeviceDriver overrides (used for testing /
                          dry_run). Missing hosts fall back to connection_pool.

        Returns:
            Final ExecutionStatus (PASSED, FAILED, ROLLED_BACK, ABORTED).
        """
        state = state_manager.get(execution_id)
        model = state.canonical_model
        device_map = device_map or {}

        logger.info("[%s] ExecutionAgent starting (dry_run=%s, steps=%d)",
                    execution_id, self._dry_run, len(model.steps))

        # 1. Build plan
        plan = PlannerAgent().plan(model, execution_id)

        # 2. Approval gate
        if plan.requires_approval:
            final_status = self._wait_for_approval(execution_id)
            if final_status is not None:
                return final_status

        # 3. Transition to RUNNING
        state_manager.transition_execution(
            execution_id, ExecutionStatus.RUNNING, agent="execution",
            message="Execution started",
        )

        # 4. Main wave loop
        try:
            final = self._run_waves(execution_id, plan, model, device_map)
        except Exception as exc:
            logger.error("[%s] Unexpected error in execution: %s", execution_id, exc)
            state_manager.transition_execution(
                execution_id, ExecutionStatus.FAILED, agent="execution",
                message=f"Unexpected error: {exc}",
            )
            return ExecutionStatus.FAILED
        finally:
            self._release_all_drivers(device_map)

        return final

    # ------------------------------------------------------------------
    # Wave execution
    # ------------------------------------------------------------------

    def _run_waves(
        self,
        execution_id: str,
        plan: ExecutionPlan,
        model: CanonicalTestModel,
        device_map: Dict[str, DeviceDriver],
    ) -> ExecutionStatus:
        """Iterate DAG waves; each wave runs its steps in parallel."""
        step_index: Dict[str, TestStep] = {s.step_id: s for s in model.steps}
        failure_strategy = model.failure_strategy or FailureStrategy.ABORT
        failed_step_ids: List[str] = []

        for wave_idx, wave in enumerate(plan.waves):
            logger.info("[%s] Wave %d/%d: %s",
                        execution_id, wave_idx + 1, len(plan.waves), wave)

            # Kill-switch check before each wave
            if kill_switch.is_set() or state_manager.is_killed(execution_id):
                logger.warning("[%s] Kill switch engaged — aborting before wave %d",
                               execution_id, wave_idx + 1)
                state_manager.transition_execution(
                    execution_id, ExecutionStatus.ABORTED, agent="execution",
                    message="Kill switch engaged",
                )
                return ExecutionStatus.ABORTED

            # Pause check
            self._wait_if_paused(execution_id)

            # Run steps in this wave in parallel
            wave_failures = self._run_wave(
                execution_id, wave, step_index, device_map
            )
            failed_step_ids.extend(wave_failures)

            if wave_failures:
                action = self._decide_on_failure(
                    execution_id, wave_failures, plan, model, device_map
                )
                if action in (ExecutionStatus.FAILED, ExecutionStatus.ABORTED):
                    state_manager.transition_execution(
                        execution_id, ExecutionStatus.FAILED, agent="execution",
                        message=f"Halted after {len(failed_step_ids)} failure(s)",
                    )
                    return ExecutionStatus.FAILED
                if action == ExecutionStatus.ROLLED_BACK:
                    state_manager.transition_execution(
                        execution_id, ExecutionStatus.ROLLED_BACK, agent="recovery",
                        message="Rollback completed",
                    )
                    return ExecutionStatus.ROLLED_BACK
                # CONTINUE: log warning and move on
                logger.warning("[%s] Continuing despite %d failure(s) in wave %d",
                               execution_id, len(wave_failures), wave_idx + 1)

        # All waves done
        if failed_step_ids:
            state_manager.transition_execution(
                execution_id, ExecutionStatus.FAILED, agent="execution",
                message=f"Completed with {len(failed_step_ids)} failed step(s)",
            )
            return ExecutionStatus.FAILED

        state_manager.transition_execution(
            execution_id, ExecutionStatus.PASSED, agent="execution",
            message="All steps completed successfully",
        )
        logger.info("[%s] Execution PASSED", execution_id)
        return ExecutionStatus.PASSED

    def _run_wave(
        self,
        execution_id: str,
        wave: List[str],
        step_index: Dict[str, TestStep],
        device_map: Dict[str, DeviceDriver],
    ) -> List[str]:
        """
        Run all steps in a wave in parallel.

        Returns list of failed step_ids (empty on full success).
        """
        if len(wave) == 1:
            # Single step — no threadpool overhead
            step = step_index[wave[0]]
            success = self._run_step(execution_id, step, device_map)
            return [] if success else [wave[0]]

        failed: List[str] = []
        with ThreadPoolExecutor(max_workers=min(len(wave), self._max_workers)) as pool:
            futures: Dict[Future, str] = {
                pool.submit(self._run_step, execution_id, step_index[sid], device_map): sid
                for sid in wave
            }
            for future in as_completed(futures):
                sid = futures[future]
                try:
                    success = future.result()
                    if not success:
                        failed.append(sid)
                except Exception as exc:
                    logger.error("[%s:%s] Unhandled exception: %s", execution_id, sid, exc)
                    failed.append(sid)

        return failed

    # ------------------------------------------------------------------
    # Single step execution
    # ------------------------------------------------------------------

    def _run_step(
        self,
        execution_id: str,
        step: TestStep,
        device_map: Dict[str, DeviceDriver],
    ) -> bool:
        """
        Execute a single step. Returns True on success, False on failure.

        Handles:
          - Kill/pause checks
          - delay_before_s timing
          - Idempotency pre-check (skip if already applied)
          - Per-retry loop with backoff
          - Validation after execution
          - delay_after_s timing
        """
        tag = f"[{execution_id}:{step.step_id}]"

        # Kill check
        if kill_switch.is_set() or state_manager.is_killed(execution_id):
            logger.warning("%s Kill switch — skipping step", tag)
            state_manager.transition_step(
                execution_id, step.step_id, ExecutionStatus.SKIPPED,
                message="Kill switch engaged",
            )
            return False

        # Per-step approval gate
        if step.approval_required:
            if not self._wait_for_step_approval(execution_id, step.step_id):
                state_manager.transition_step(
                    execution_id, step.step_id, ExecutionStatus.SKIPPED,
                    message="Step approval denied or timed out",
                )
                return False

        # delay_before
        if step.timing.delay_before_s > 0:
            logger.debug("%s delay_before %.1fs", tag, step.timing.delay_before_s)
            time.sleep(step.timing.delay_before_s)

        # Acquire driver for the primary device (first device or "default")
        driver = self._get_driver(step, device_map, execution_id)

        # Idempotency pre-check
        if step.idempotency_rules and driver:
            result = idempotency_engine_module.check(
                step.idempotency_rules, driver,
                step_id=step.step_id, execution_id=execution_id,
            )
            if result.verdict == "skip":
                logger.info("%s Idempotency SKIP — already applied", tag)
                state_manager.transition_step(
                    execution_id, step.step_id, ExecutionStatus.SKIPPED,
                    message=f"Idempotency skip: {result.rule_fired}",
                )
                return True
            if result.verdict == "partial_state":
                logger.warning("%s Idempotency PARTIAL_STATE — proceeding with caution", tag)

        # Mark step as RUNNING
        primary_device = step.devices[0].hostname if step.devices else "default"
        state_manager.transition_step(
            execution_id, step.step_id, ExecutionStatus.RUNNING,
            device=primary_device, message="Step started",
        )

        # Retry loop
        policy = step.execution_policy
        last_output = ""
        last_error = ""
        success = False

        for attempt in range(1, policy.retry_count + 1):
            if kill_switch.is_set() or state_manager.is_killed(execution_id):
                break

            try:
                output = self._execute_commands(step, driver, execution_id)
                last_output = output

                # Validate
                val = self._validator.validate(
                    step, output, driver=driver, execution_id=execution_id
                )

                if val.passed:
                    success = True
                    break
                else:
                    last_error = "; ".join(val.errors)
                    logger.warning("%s Attempt %d/%d — validation failed: %s",
                                   tag, attempt, policy.retry_count, last_error)

            except (DeviceCommandError, DeviceConnectionError) as exc:
                last_error = str(exc)
                logger.warning("%s Attempt %d/%d — command error: %s",
                               tag, attempt, policy.retry_count, last_error)

            # Backoff before retry
            if attempt < policy.retry_count:
                time.sleep(policy.retry_delay_s)

        # Persist result
        if success:
            state_manager.transition_step(
                execution_id, step.step_id, ExecutionStatus.PASSED,
                actual_output=last_output,
                validation_passed=True,
                message="Step completed",
            )
        else:
            state_manager.transition_step(
                execution_id, step.step_id, ExecutionStatus.FAILED,
                actual_output=last_output,
                error_message=last_error,
                validation_passed=False,
                message=f"Failed after {policy.retry_count} attempt(s): {last_error}",
            )

        # delay_after
        if step.timing.delay_after_s > 0:
            logger.debug("%s delay_after %.1fs", tag, step.timing.delay_after_s)
            time.sleep(step.timing.delay_after_s)

        return success

    def _execute_commands(
        self,
        step: TestStep,
        driver: Optional[DeviceDriver],
        execution_id: str,
    ) -> str:
        """Send all commands in the step and concatenate outputs."""
        if not step.commands:
            return ""

        if driver is None:
            # Dry-run: no driver for this device — return placeholder
            return f"[DRY RUN — no driver for step {step.step_id}]"

        outputs: List[str] = []
        timeout = step.execution_policy.timeout_s
        hostname = step.devices[0].hostname if step.devices else "default"

        for cmd in step.commands:
            raw = cmd.raw.strip()
            if not raw:
                continue
            logger.debug("[%s:%s] CMD: %s", execution_id, step.step_id, raw[:80])
            with concurrency_controller.acquire_device(hostname):
                output = driver.execute(raw, timeout_s=timeout)
            outputs.append(f"# {raw}\n{output}")

            # Smart wait: if the step expects polling (validation rules with max_wait)
            # that is handled separately by the PollingEngine in _run_step.

        return "\n\n".join(outputs)

    # ------------------------------------------------------------------
    # Driver management
    # ------------------------------------------------------------------

    def _get_driver(
        self,
        step: TestStep,
        device_map: Dict[str, DeviceDriver],
        execution_id: str,
    ) -> Optional[DeviceDriver]:
        """
        Return the driver for the step's primary device.

        Priority:
          1. Explicit device_map override
          2. connection_pool (for live execution)
          3. MockDriver (for dry_run)
          4. None (no device — execute-only steps that don't need SSH)
        """
        if not step.devices:
            if self._dry_run:
                return MockDriver(hostname="default")
            return None

        hostname = step.devices[0].hostname

        if hostname in device_map:
            drv = device_map[hostname]
            if not drv.is_connected:
                drv.connect()
            return drv

        if self._dry_run:
            drv = MockDriver(hostname=hostname)
            drv.connect()
            return drv

        # Live: use connection pool (acquires or reuses SSH session)
        vendor = (step.commands[0].vendor if step.commands else "generic") or "generic"
        try:
            return connection_pool.acquire(hostname, vendor=vendor)
        except Exception as exc:
            logger.error("[%s:%s] Cannot acquire connection to %s: %s",
                         execution_id, step.step_id, hostname, exc)
            return None

    def _release_all_drivers(self, device_map: Dict[str, DeviceDriver]) -> None:
        """Return pool-managed connections; explicit overrides are caller-managed."""
        # connection_pool handles its own idle sweep — we just note the release intent.
        # Drivers in device_map are owned by the caller.
        pass

    # ------------------------------------------------------------------
    # Approval gates
    # ------------------------------------------------------------------

    def _wait_for_approval(
        self, execution_id: str, timeout_s: float = 3600.0
    ) -> Optional[ExecutionStatus]:
        """
        Block until execution-level approval is granted or denied.

        Returns None (continue) or ExecutionStatus.ABORTED (denied/timeout).
        """
        state_manager.transition_execution(
            execution_id, ExecutionStatus.AWAITING_APPROVAL, agent="execution",
            message="Waiting for human approval before execution starts",
        )
        logger.info("[%s] Awaiting execution-level approval (timeout=%.0fs)",
                    execution_id, timeout_s)

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if kill_switch.is_set() or state_manager.is_killed(execution_id):
                return ExecutionStatus.ABORTED
            state = state_manager.get(execution_id)
            if state.approval_status == ApprovalStatus.APPROVED:
                logger.info("[%s] Approval granted by %s", execution_id, state.approver_id)
                return None
            if state.approval_status == ApprovalStatus.DENIED:
                logger.warning("[%s] Approval denied by %s", execution_id, state.approver_id)
                state_manager.transition_execution(
                    execution_id, ExecutionStatus.ABORTED, agent="execution",
                    message="Approval denied",
                )
                return ExecutionStatus.ABORTED
            time.sleep(_PAUSE_POLL_INTERVAL_S)

        logger.error("[%s] Approval timeout after %.0fs", execution_id, timeout_s)
        state_manager.transition_execution(
            execution_id, ExecutionStatus.ABORTED, agent="execution",
            message="Approval timed out",
        )
        return ExecutionStatus.ABORTED

    def _wait_for_step_approval(
        self, execution_id: str, step_id: str, timeout_s: float = 1800.0
    ) -> bool:
        """
        Wait for per-step approval. Returns True (approved) or False (denied/timeout).
        """
        state_manager.transition_step(
            execution_id, step_id, ExecutionStatus.AWAITING_APPROVAL,
            message="Waiting for step-level approval",
        )
        logger.info("[%s:%s] Awaiting step approval", execution_id, step_id)

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if kill_switch.is_set() or state_manager.is_killed(execution_id):
                return False
            state = state_manager.get(execution_id)
            step_result = state.steps.get(step_id)
            # Approval is conveyed by transitioning step back to PENDING + setting flag
            # via the API layer. Here we poll for it.
            if step_result and step_result.status == ExecutionStatus.PENDING:
                return True  # API reset to PENDING = approved
            time.sleep(_PAUSE_POLL_INTERVAL_S)

        return False

    # ------------------------------------------------------------------
    # Pause / resume
    # ------------------------------------------------------------------

    def _wait_if_paused(self, execution_id: str) -> None:
        """Block while the execution is in PAUSED state."""
        while True:
            state = state_manager.get(execution_id)
            if not state.paused:
                break
            if kill_switch.is_set() or state.kill_requested:
                break
            logger.debug("[%s] Execution paused — sleeping", execution_id)
            time.sleep(_PAUSE_POLL_INTERVAL_S)

    # ------------------------------------------------------------------
    # Failure decision
    # ------------------------------------------------------------------

    def _decide_on_failure(
        self,
        execution_id: str,
        failed_step_ids: List[str],
        plan: ExecutionPlan,
        model: CanonicalTestModel,
        device_map: Dict[str, DeviceDriver],
    ) -> ExecutionStatus:
        """
        Decide what to do after one or more steps fail in a wave.

        Returns:
          FAILED/ABORTED — stop execution
          ROLLED_BACK     — rollback completed
          RUNNING         — continue (failure_strategy=CONTINUE)
        """
        from execution_engine.recovery_agent import RecoveryAgent

        strategy = model.failure_strategy or FailureStrategy.ABORT
        recovery = RecoveryAgent()

        if strategy == FailureStrategy.CONTINUE:
            logger.info("[%s] FailureStrategy=CONTINUE — ignoring %d failure(s)",
                        execution_id, len(failed_step_ids))
            return ExecutionStatus.RUNNING

        if strategy == FailureStrategy.ROLLBACK_GROUP:
            # Find which transaction groups the failed steps belong to
            rolled_back = False
            for step_id in failed_step_ids:
                step = next((s for s in model.steps if s.step_id == step_id), None)
                if step and step.transaction_group:
                    group = plan.transaction_groups.get(step.transaction_group, [])
                    if group:
                        recovery.rollback_group(
                            step.transaction_group, group, model, execution_id, device_map
                        )
                        rolled_back = True
            if rolled_back:
                return ExecutionStatus.ROLLED_BACK
            # No group found — fall through to ABORT
            logger.warning("[%s] ROLLBACK_GROUP but no transaction_group assigned — aborting",
                           execution_id)
            return ExecutionStatus.FAILED

        if strategy == FailureStrategy.ROLLBACK_ALL:
            recovery.rollback_all(execution_id, model, device_map)
            return ExecutionStatus.ROLLED_BACK

        # Default: ABORT
        return ExecutionStatus.FAILED
