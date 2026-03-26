"""
State Manager — single source of truth for all execution state.

All agents read and write execution state ONLY through this module.
No agent holds its own in-memory copy of execution state.

Storage: output/executions/{execution_id}.json
Locking: threading.RLock per execution_id (re-entrant for nested calls)
TTL:     168 hours default; expired states archived to output/executions/archive/
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from models.canonical import ApprovalStatus, CanonicalTestModel, ExecutionStatus
from execution_engine.models import ExecutionState, StepResult, TransitionRecord

logger = logging.getLogger(__name__)

_EXEC_DIR    = Path("output") / "executions"
_LOCK_DIR    = _EXEC_DIR / "locks"
_ARCHIVE_DIR = _EXEC_DIR / "archive"
_DEFAULT_TTL_HOURS = 168


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _exec_path(execution_id: str) -> Path:
    return _EXEC_DIR / f"{execution_id}.json"


def _lock_path(execution_id: str) -> Path:
    return _LOCK_DIR / f"{execution_id}.lock"


class StateManager:
    """
    Thread-safe execution state store.

    One instance shared across the whole application (module-level singleton).
    """

    def __init__(self, ttl_hours: int = _DEFAULT_TTL_HOURS) -> None:
        self._locks: Dict[str, threading.RLock] = {}
        self._meta_lock = threading.Lock()   # protects self._locks dict
        self._ttl_hours = ttl_hours
        for d in (_EXEC_DIR, _LOCK_DIR, _ARCHIVE_DIR):
            d.mkdir(parents=True, exist_ok=True)
        self._start_sweep_thread()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(
        self,
        canonical_model: CanonicalTestModel,
        dry_run: bool = False,
    ) -> str:
        """Create a new execution record and return its execution_id."""
        import uuid
        execution_id = str(uuid.uuid4())[:12]
        state = ExecutionState(
            execution_id=execution_id,
            canonical_model=canonical_model,
            status=ExecutionStatus.PENDING,
            created_at=_now(),
            correlation_id=execution_id,
            dry_run=dry_run,
        )
        # Initialise per-step result records
        for step in canonical_model.steps:
            state.steps[step.step_id] = StepResult(step_id=step.step_id)

        self._write(state)
        _lock_path(execution_id).touch()
        logger.info("[%s] Execution created (dry_run=%s, steps=%d)",
                    execution_id, dry_run, len(canonical_model.steps))
        return execution_id

    def get(self, execution_id: str) -> ExecutionState:
        """Return a copy of the current ExecutionState. Raises KeyError if not found."""
        path = _exec_path(execution_id)
        if not path.exists():
            raise KeyError(f"Execution not found: {execution_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return ExecutionState.model_validate(data)

    def transition_execution(
        self,
        execution_id: str,
        new_status: ExecutionStatus,
        agent: str = "system",
        message: str = "",
    ) -> None:
        """Atomically update execution-level status and append history record."""
        with self._lock(execution_id):
            state = self.get(execution_id)
            old_status = state.status.value
            state.status = new_status
            if new_status == ExecutionStatus.RUNNING and not state.started_at:
                state.started_at = _now()
            if new_status in (ExecutionStatus.PASSED, ExecutionStatus.FAILED,
                              ExecutionStatus.ROLLED_BACK, ExecutionStatus.ABORTED):
                state.completed_at = _now()
                lp = _lock_path(execution_id)
                if lp.exists():
                    lp.unlink()
            state.history.append(TransitionRecord(
                timestamp=_now(),
                entity="execution",
                from_status=old_status,
                to_status=new_status.value,
                agent=agent,
                message=message or f"{old_status} → {new_status.value}",
                correlation_id=execution_id,
            ))
            self._write(state)
        logger.info("[%s] Execution %s → %s  (%s)", execution_id, old_status, new_status.value, agent)

    def transition_step(
        self,
        execution_id: str,
        step_id: str,
        new_status: ExecutionStatus,
        agent: str = "execution",
        message: str = "",
        actual_output: Optional[str] = None,
        error_message: Optional[str] = None,
        validation_passed: Optional[bool] = None,
        validation_errors: Optional[List[str]] = None,
        device: Optional[str] = None,
    ) -> None:
        """Atomically update a single step's status and write to history."""
        with self._lock(execution_id):
            state = self.get(execution_id)
            result = state.steps.get(step_id, StepResult(step_id=step_id))
            old_status = result.status.value

            result.status = new_status
            if new_status == ExecutionStatus.RUNNING:
                result.started_at = _now()
                result.attempts += 1
                if device:
                    result.device = device
            if new_status in (ExecutionStatus.PASSED, ExecutionStatus.FAILED,
                              ExecutionStatus.SKIPPED, ExecutionStatus.ROLLED_BACK):
                result.completed_at = _now()
                if result.started_at:
                    start = datetime.fromisoformat(result.started_at)
                    result.duration_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
            if actual_output is not None:
                result.actual_output = actual_output
            if error_message is not None:
                result.error_message = error_message
            if validation_passed is not None:
                result.validation_passed = validation_passed
            if validation_errors is not None:
                result.validation_errors = validation_errors

            state.steps[step_id] = result
            state.history.append(TransitionRecord(
                timestamp=_now(),
                entity=step_id,
                from_status=old_status,
                to_status=new_status.value,
                agent=agent,
                message=message or f"step {old_status} → {new_status.value}",
                correlation_id=f"{execution_id}:{step_id}:{result.attempts}",
            ))
            self._write(state)
        logger.debug("[%s:%s] %s → %s", execution_id, step_id, old_status, new_status.value)

    def update_field(self, execution_id: str, **kwargs) -> None:
        """Set arbitrary top-level fields on the ExecutionState."""
        with self._lock(execution_id):
            state = self.get(execution_id)
            for k, v in kwargs.items():
                if hasattr(state, k):
                    setattr(state, k, v)
            self._write(state)

    def set_approval(
        self,
        execution_id: str,
        approver_id: str,
        decision: ApprovalStatus,
    ) -> None:
        with self._lock(execution_id):
            state = self.get(execution_id)
            state.approval_status = decision
            state.approver_id = approver_id
            state.approved_at = _now()
            state.history.append(TransitionRecord(
                timestamp=_now(),
                entity="execution",
                from_status=state.status.value,
                to_status=state.status.value,
                agent="api",
                message=f"Approval {decision.value} by {approver_id}",
                correlation_id=execution_id,
            ))
            self._write(state)
        logger.info("[%s] Approval %s by %s", execution_id, decision.value, approver_id)

    def request_kill(self, execution_id: Optional[str] = None) -> None:
        """Mark one execution (or all) as kill_requested."""
        if execution_id:
            with self._lock(execution_id):
                state = self.get(execution_id)
                state.kill_requested = True
                self._write(state)
        else:
            for path in _EXEC_DIR.glob("*.json"):
                eid = path.stem
                try:
                    with self._lock(eid):
                        state = self.get(eid)
                        if state.status == ExecutionStatus.RUNNING:
                            state.kill_requested = True
                            self._write(state)
                except Exception:
                    pass

    def is_killed(self, execution_id: str) -> bool:
        try:
            return self.get(execution_id).kill_requested
        except KeyError:
            return False

    def list_executions(self, limit: int = 100) -> List[dict]:
        """Return summary dicts for recent executions, newest first."""
        results = []
        for path in sorted(_EXEC_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                results.append({
                    "execution_id": data.get("execution_id"),
                    "status": data.get("status"),
                    "document_title": data.get("canonical_model", {}).get("document_title"),
                    "created_at": data.get("created_at"),
                    "completed_at": data.get("completed_at"),
                    "dry_run": data.get("dry_run", False),
                    "steps_total": len(data.get("steps", {})),
                    "steps_passed": sum(1 for s in data.get("steps", {}).values() if s.get("status") == "passed"),
                })
            except Exception:
                continue
        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _lock(self, execution_id: str) -> threading.RLock:
        with self._meta_lock:
            if execution_id not in self._locks:
                self._locks[execution_id] = threading.RLock()
            return self._locks[execution_id]

    def _write(self, state: ExecutionState) -> None:
        path = _exec_path(state.execution_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)  # atomic rename

    def _start_sweep_thread(self) -> None:
        def _sweep():
            while True:
                time.sleep(3600)  # check hourly
                cutoff = datetime.now(timezone.utc) - timedelta(hours=self._ttl_hours)
                for path in _EXEC_DIR.glob("*.json"):
                    try:
                        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                        if mtime < cutoff:
                            dest = _ARCHIVE_DIR / path.name
                            shutil.move(str(path), str(dest))
                            logger.info("Archived expired execution: %s", path.name)
                    except Exception:
                        pass

        t = threading.Thread(target=_sweep, daemon=True, name="state-manager-sweep")
        t.start()


# Module-level singleton
state_manager = StateManager()
