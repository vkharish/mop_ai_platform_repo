"""
PagerDuty Notifier — triggers/resolves incidents via Events API v2.

Enable: set PD_INTEGRATION_KEY env var.
Dry-run: if env var missing, logs with [NOTIFICATION_DRY_RUN] prefix.

Trigger events: execution_failed, rollback_failed, kill_switch_engaged
Resolve events: execution_passed, rollback_passed
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

_PD_URL = "https://events.pagerduty.com/v2/enqueue"
_TRIGGER_EVENTS = {"execution_failed", "rollback_failed", "kill_switch_engaged"}
_RESOLVE_EVENTS = {"execution_passed", "rollback_passed"}


class PagerDutyNotifier:

    def __init__(self) -> None:
        self._key = os.environ.get("PD_INTEGRATION_KEY", "")

    def send(self, event: str, **kwargs) -> bool:
        if event not in _TRIGGER_EVENTS and event not in _RESOLVE_EVENTS:
            return False

        action = "trigger" if event in _TRIGGER_EVENTS else "resolve"
        execution_id = kwargs.get("execution_id", "unknown")
        summary = f"MOP [{event}] exec={execution_id}"
        if "title" in kwargs:
            summary = f"MOP [{event}] {kwargs['title']} (exec={execution_id})"

        payload = {
            "routing_key": self._key,
            "event_action": action,
            "dedup_key": f"mop-{execution_id}",
            "payload": {
                "summary": summary,
                "severity": "critical" if event in ("kill_switch_engaged", "rollback_failed") else "error",
                "source": "mop-ai-platform",
                "custom_details": {k: str(v)[:200] for k, v in kwargs.items()},
            },
        }

        if not self._key:
            logger.info("[NOTIFICATION_DRY_RUN] PagerDuty %s: %s", action, summary)
            return False

        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                _PD_URL,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                ok = resp.status == 202
            logger.debug("PagerDuty %s sent: %s", action, "ok" if ok else "fail")
            return ok
        except Exception as exc:
            logger.warning("PagerDuty notification failed: %s", exc)
            return False
