"""
API v2 — Execution Engine endpoints.

Mounts at /api/v2/ in api/main.py.

Endpoints:
  POST   /executions                    Start new execution
  GET    /executions                    List executions
  GET    /executions/{id}               Get execution state
  POST   /executions/{id}/pause         Pause execution
  POST   /executions/{id}/resume        Resume execution
  POST   /executions/{id}/abort         Abort execution
  POST   /executions/{id}/rollback      Trigger rollback
  GET    /executions/{id}/report        Get execution report (JSON)
  GET    /executions/{id}/report/html   Get execution report (HTML)
  GET    /executions/{id}/timeline      Get execution timeline
  POST   /approvals/{id}                Submit approval decision
  POST   /kill                          Engage global kill switch
  DELETE /kill                          Clear global kill switch
  GET    /kill                          Get kill switch status
  GET    /metrics                       Prometheus-compatible metrics
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from models.canonical import ApprovalStatus, CanonicalTestModel, ExecutionStatus
from execution_engine.kill_switch import kill_switch
from execution_engine.state_manager import state_manager
from safety.rbac import require_role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2", tags=["execution-v2"])

# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class StartExecutionRequest(BaseModel):
    canonical_model:  CanonicalTestModel
    dry_run:          bool = False
    device_overrides: Dict[str, Any] = Field(default_factory=dict)


class ApprovalRequest(BaseModel):
    approver_id: str
    decision:    str  # "approved" | "denied"
    comment:     str = ""


class PauseRequest(BaseModel):
    reason: str = ""


# ---------------------------------------------------------------------------
# POST /api/v2/executions
# ---------------------------------------------------------------------------

@router.post("/executions", status_code=202)
async def start_execution(
    request: StartExecutionRequest,
    background_tasks: BackgroundTasks,
    _role = Depends(require_role("executor")),
):
    """
    Create and start a new execution.

    The execution runs in a background thread. Returns execution_id immediately.
    Poll GET /api/v2/executions/{id} for status.
    """
    model = request.canonical_model
    dry_run = request.dry_run

    # Check kill switch
    if kill_switch.is_set():
        raise HTTPException(
            status_code=503,
            detail="Kill switch is engaged. Clear it before starting new executions.",
        )

    # Create execution state
    execution_id = state_manager.create(model, dry_run=dry_run)
    logger.info("Execution created via API: %s (dry_run=%s)", execution_id, dry_run)

    # Launch in background thread
    def _run():
        from execution_engine.execution_agent import ExecutionAgent
        try:
            agent = ExecutionAgent(dry_run=dry_run)
            agent.run(execution_id)
        except Exception as exc:
            logger.error("[%s] Background execution error: %s", execution_id, exc)
            try:
                state_manager.transition_execution(
                    execution_id, ExecutionStatus.FAILED, agent="system",
                    message=f"Unhandled error: {exc}",
                )
            except Exception:
                pass
        finally:
            # Generate report on completion
            try:
                from reporting.execution_report import ExecutionReportBuilder
                ExecutionReportBuilder.save(execution_id)
            except Exception as exc:
                logger.warning("[%s] Report generation failed: %s", execution_id, exc)
            # ITSM update
            try:
                state = state_manager.get(execution_id)
                if state.canonical_model.change_ticket:
                    from itsm.itsm_client import itsm_client
                    ticket = state.canonical_model.change_ticket
                    failed = [sid for sid, r in state.steps.items() if r.status == ExecutionStatus.FAILED]
                    if state.status == ExecutionStatus.PASSED:
                        itsm_client.notify_execution_passed(ticket, execution_id, 0)
                    else:
                        itsm_client.notify_execution_failed(ticket, execution_id, failed)
                    state_manager.update_field(execution_id, itsm_updated=True)
            except Exception as exc:
                logger.warning("[%s] ITSM update failed: %s", execution_id, exc)

    t = threading.Thread(target=_run, daemon=True, name=f"exec-{execution_id}")
    t.start()

    return {
        "execution_id": execution_id,
        "status": "pending",
        "dry_run": dry_run,
        "message": "Execution started. Poll GET /api/v2/executions/{id} for status.",
    }


# ---------------------------------------------------------------------------
# GET /api/v2/executions
# ---------------------------------------------------------------------------

@router.get("/executions")
async def list_executions(
    limit: int = 50,
    _role = Depends(require_role("reader")),
):
    return state_manager.list_executions(limit=limit)


# ---------------------------------------------------------------------------
# GET /api/v2/executions/{execution_id}
# ---------------------------------------------------------------------------

@router.get("/executions/{execution_id}")
async def get_execution(
    execution_id: str,
    _role = Depends(require_role("reader")),
):
    try:
        state = state_manager.get(execution_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Execution not found: {execution_id}")
    return state.model_dump(mode="json")


# ---------------------------------------------------------------------------
# POST /api/v2/executions/{execution_id}/pause
# ---------------------------------------------------------------------------

@router.post("/executions/{execution_id}/pause")
async def pause_execution(
    execution_id: str,
    body: PauseRequest = PauseRequest(),
    _role = Depends(require_role("executor")),
):
    try:
        state = state_manager.get(execution_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Execution not found")
    if state.status != ExecutionStatus.RUNNING:
        raise HTTPException(status_code=409, detail=f"Cannot pause: status is {state.status.value}")
    state_manager.update_field(execution_id, paused=True, pause_reason=body.reason)
    return {"execution_id": execution_id, "paused": True}


# ---------------------------------------------------------------------------
# POST /api/v2/executions/{execution_id}/resume
# ---------------------------------------------------------------------------

@router.post("/executions/{execution_id}/resume")
async def resume_execution(
    execution_id: str,
    _role = Depends(require_role("executor")),
):
    try:
        state = state_manager.get(execution_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Execution not found")
    state_manager.update_field(execution_id, paused=False, pause_reason=None)
    return {"execution_id": execution_id, "paused": False}


# ---------------------------------------------------------------------------
# POST /api/v2/executions/{execution_id}/abort
# ---------------------------------------------------------------------------

@router.post("/executions/{execution_id}/abort")
async def abort_execution(
    execution_id: str,
    _role = Depends(require_role("executor")),
):
    try:
        state_manager.request_kill(execution_id)
        state_manager.transition_execution(
            execution_id, ExecutionStatus.ABORTED, agent="api", message="Aborted via API"
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Execution not found")
    return {"execution_id": execution_id, "status": "aborted"}


# ---------------------------------------------------------------------------
# POST /api/v2/executions/{execution_id}/rollback
# ---------------------------------------------------------------------------

@router.post("/executions/{execution_id}/rollback")
async def trigger_rollback(
    execution_id: str,
    background_tasks: BackgroundTasks,
    _role = Depends(require_role("executor")),
):
    try:
        state = state_manager.get(execution_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Execution not found")
    if state.status not in (ExecutionStatus.FAILED, ExecutionStatus.PAUSED):
        raise HTTPException(status_code=409,
                            detail=f"Rollback only valid from FAILED/PAUSED state, not {state.status.value}")

    def _rollback():
        from execution_engine.recovery_agent import RecoveryAgent
        model = state.canonical_model
        try:
            RecoveryAgent().rollback_all(execution_id, model, {})
            state_manager.transition_execution(
                execution_id, ExecutionStatus.ROLLED_BACK, agent="api",
                message="Rollback triggered via API",
            )
        except Exception as exc:
            logger.error("[%s] Manual rollback error: %s", execution_id, exc)

    background_tasks.add_task(_rollback)
    return {"execution_id": execution_id, "message": "Rollback initiated"}


# ---------------------------------------------------------------------------
# GET /api/v2/executions/{execution_id}/report
# ---------------------------------------------------------------------------

@router.get("/executions/{execution_id}/report")
async def get_report_json(
    execution_id: str,
    _role = Depends(require_role("reader")),
):
    try:
        state_manager.get(execution_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Execution not found")
    from reporting.execution_report import ExecutionReportBuilder
    return ExecutionReportBuilder.build(execution_id)


@router.get("/executions/{execution_id}/report/html")
async def get_report_html(
    execution_id: str,
    _role = Depends(require_role("reader")),
):
    try:
        state_manager.get(execution_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Execution not found")
    from reporting.execution_report import ExecutionReportBuilder
    report = ExecutionReportBuilder.build(execution_id)
    html = ExecutionReportBuilder.render_html(report)
    return Response(content=html, media_type="text/html")


# ---------------------------------------------------------------------------
# GET /api/v2/executions/{execution_id}/timeline
# ---------------------------------------------------------------------------

@router.get("/executions/{execution_id}/timeline")
async def get_timeline(
    execution_id: str,
    _role = Depends(require_role("reader")),
):
    try:
        state = state_manager.get(execution_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Execution not found")
    return sorted(
        [h.model_dump() for h in state.history],
        key=lambda x: x.get("timestamp", ""),
    )


# ---------------------------------------------------------------------------
# POST /api/v2/approvals/{execution_id}
# ---------------------------------------------------------------------------

@router.post("/approvals/{execution_id}")
async def submit_approval(
    execution_id: str,
    body: ApprovalRequest,
    _role = Depends(require_role("approver")),
):
    try:
        state = state_manager.get(execution_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Execution not found")
    if state.approval_status not in (ApprovalStatus.PENDING, ApprovalStatus.NOT_REQUIRED):
        raise HTTPException(status_code=409,
                            detail=f"Approval not applicable in state: {state.approval_status.value}")

    decision_map = {"approved": ApprovalStatus.APPROVED, "denied": ApprovalStatus.REJECTED}
    decision = decision_map.get(body.decision.lower())
    if not decision:
        raise HTTPException(status_code=400, detail="decision must be 'approved' or 'denied'")

    state_manager.set_approval(execution_id, approver_id=body.approver_id, decision=decision)
    return {
        "execution_id": execution_id,
        "decision": body.decision,
        "approver_id": body.approver_id,
    }


# ---------------------------------------------------------------------------
# Kill switch endpoints
# ---------------------------------------------------------------------------

@router.post("/kill")
async def engage_kill_switch(
    _role = Depends(require_role("admin")),
):
    kill_switch.engage(reason="API request")
    state_manager.request_kill()  # mark all running executions
    try:
        from notifications.notification_router import notification_router
        notification_router.send_kill_switch(execution_id=None, reason="Engaged via API")
    except Exception:
        pass
    return {"kill_switch": "engaged"}


@router.delete("/kill")
async def clear_kill_switch(
    _role = Depends(require_role("admin")),
):
    kill_switch.clear()
    return {"kill_switch": "cleared"}


@router.get("/kill")
async def get_kill_switch_status(
    _role = Depends(require_role("reader")),
):
    return {"kill_switch_engaged": kill_switch.is_set()}


# ---------------------------------------------------------------------------
# GET /api/v2/metrics
# ---------------------------------------------------------------------------

@router.get("/metrics", response_class=Response)
async def get_metrics(
    _role = Depends(require_role("reader")),
):
    """Prometheus-compatible text/plain metrics."""
    executions = state_manager.list_executions(limit=500)

    counts: Dict[str, int] = {}
    for e in executions:
        s = e.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1

    active = sum(1 for e in executions if e.get("status") == "running")

    lines = [
        "# HELP mop_executions_total Total executions by status",
        "# TYPE mop_executions_total counter",
    ]
    for status, count in counts.items():
        lines.append(f'mop_executions_total{{status="{status}"}} {count}')

    lines += [
        "# HELP mop_active_executions Currently running executions",
        "# TYPE mop_active_executions gauge",
        f"mop_active_executions {active}",
    ]

    text = "\n".join(lines) + "\n"
    return Response(content=text, media_type="text/plain; version=0.0.4")
