"""
ITSM Client — routes ticket updates to the correct backend adapter.

Supported: ServiceNow (system="servicenow"), Jira (system="jira")

Usage:
    from itsm.itsm_client import itsm_client
    itsm_client.comment(ticket_ref, "Execution started: exec_id=abc123")
    itsm_client.transition(ticket_ref, "In Progress")
"""

from __future__ import annotations

import logging
from typing import Optional

from models.canonical import ITSMRef

logger = logging.getLogger(__name__)


class ITSMClient:
    """
    Facade that delegates to the correct adapter based on ITSMRef.system.
    Failures are caught and logged — never propagate to execution.
    """

    def comment(self, ticket: ITSMRef, text: str) -> bool:
        try:
            adapter = self._get_adapter(ticket.system)
            return adapter.add_comment(ticket.ticket_id, ticket.webhook_url, text)
        except Exception as exc:
            logger.error("[ITSM] Comment failed (%s/%s): %s", ticket.system, ticket.ticket_id, exc)
            return False

    def transition(self, ticket: ITSMRef, state: str) -> bool:
        try:
            adapter = self._get_adapter(ticket.system)
            return adapter.transition(ticket.ticket_id, ticket.webhook_url, state)
        except Exception as exc:
            logger.error("[ITSM] Transition failed (%s/%s → %s): %s",
                         ticket.system, ticket.ticket_id, state, exc)
            return False

    def notify_execution_started(self, ticket: ITSMRef, execution_id: str) -> bool:
        return self.comment(ticket, f"[MOP Platform] Execution started: {execution_id}")

    def notify_step_failed(self, ticket: ITSMRef, step_id: str, device: str, error: str) -> bool:
        snippet = error[:300] if error else ""
        return self.comment(ticket,
            f"[MOP Platform] Step failed: step={step_id} device={device}\nError: {snippet}")

    def notify_execution_passed(self, ticket: ITSMRef, execution_id: str, duration_s: float) -> bool:
        return self.comment(ticket,
            f"[MOP Platform] Execution PASSED: {execution_id} (duration={duration_s:.1f}s)")

    def notify_execution_failed(self, ticket: ITSMRef, execution_id: str, failed_steps: list) -> bool:
        return self.comment(ticket,
            f"[MOP Platform] Execution FAILED: {execution_id}\n"
            f"Failed steps: {', '.join(str(s) for s in failed_steps[:10])}")

    def notify_rollback_completed(self, ticket: ITSMRef, execution_id: str) -> bool:
        return self.comment(ticket,
            f"[MOP Platform] Rollback completed for execution: {execution_id}")

    def _get_adapter(self, system: str):
        system = system.lower()
        if system == "servicenow":
            from itsm.servicenow_adapter import ServiceNowAdapter
            return ServiceNowAdapter()
        if system == "jira":
            from itsm.jira_adapter import JiraAdapter
            return JiraAdapter()
        raise ValueError(f"Unsupported ITSM system: '{system}'")


# Module-level singleton
itsm_client = ITSMClient()
