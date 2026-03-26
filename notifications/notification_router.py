"""
Notification Router — dispatches lifecycle events to all enabled channels.

Channels are enabled via env vars (see each notifier). Missing env vars → dry-run mode.

Events dispatched:
  execution_started, approval_required, step_failed_with_retry,
  step_failed_no_retry, execution_passed, execution_failed,
  rollback_started, rollback_passed, rollback_failed,
  kill_switch_engaged, maintenance_window_expiring

Usage:
    from notifications.notification_router import notification_router
    notification_router.send("execution_started", execution_id="abc123", title="BGP MOP")
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class NotificationRouter:
    """
    Loads all notifiers at import time, dispatches to all enabled ones.
    Notifier failures are caught and logged — never propagate to execution.
    """

    def __init__(self) -> None:
        from notifications.slack_notifier import SlackNotifier
        from notifications.email_notifier import EmailNotifier
        from notifications.pagerduty_notifier import PagerDutyNotifier
        self._notifiers = [SlackNotifier(), EmailNotifier(), PagerDutyNotifier()]

    def send(self, event: str, **kwargs) -> Dict[str, bool]:
        """
        Dispatch event to all notifiers. Returns {notifier_name: sent_bool}.

        kwargs passed to each notifier's send() method as context.
        """
        results = {}
        for notifier in self._notifiers:
            name = notifier.__class__.__name__
            try:
                sent = notifier.send(event, **kwargs)
                results[name] = sent
            except Exception as exc:
                logger.error("[NotificationRouter] %s raised: %s", name, exc)
                results[name] = False
        return results

    def send_execution_started(self, execution_id: str, title: str, steps: int, dry_run: bool = False) -> None:
        self.send("execution_started", execution_id=execution_id, title=title, steps=steps, dry_run=dry_run)

    def send_execution_passed(self, execution_id: str, title: str, duration_s: float, steps_passed: int) -> None:
        self.send("execution_passed", execution_id=execution_id, title=title, duration_s=duration_s, steps_passed=steps_passed)

    def send_execution_failed(self, execution_id: str, title: str, failed_steps: List[str], error: str = "") -> None:
        self.send("execution_failed", execution_id=execution_id, title=title, failed_steps=failed_steps, error=error)

    def send_step_failed(self, execution_id: str, step_id: str, device: str, error: str, will_retry: bool) -> None:
        event = "step_failed_with_retry" if will_retry else "step_failed_no_retry"
        self.send(event, execution_id=execution_id, step_id=step_id, device=device, error=error)

    def send_rollback_started(self, execution_id: str, scope: str) -> None:
        self.send("rollback_started", execution_id=execution_id, scope=scope)

    def send_rollback_result(self, execution_id: str, success: bool) -> None:
        event = "rollback_passed" if success else "rollback_failed"
        self.send(event, execution_id=execution_id)

    def send_kill_switch(self, execution_id: Optional[str], reason: str) -> None:
        self.send("kill_switch_engaged", execution_id=execution_id or "ALL", reason=reason)

    def send_approval_required(self, execution_id: str, title: str, reasons: List[str]) -> None:
        self.send("approval_required", execution_id=execution_id, title=title, reasons=reasons)


# Module-level singleton
notification_router = NotificationRouter()
