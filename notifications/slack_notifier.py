"""
Slack Notifier — posts to a Slack webhook URL.

Enable: set SLACK_WEBHOOK_URL env var.
Dry-run: if env var missing, logs with [NOTIFICATION_DRY_RUN] prefix.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

_COLORS = {
    "execution_started": "#36a64f",
    "execution_passed": "#36a64f",
    "approval_required": "#ffb700",
    "step_failed_with_retry": "#ff9900",
    "step_failed_no_retry": "#cc0000",
    "execution_failed": "#cc0000",
    "rollback_started": "#ffb700",
    "rollback_passed": "#36a64f",
    "rollback_failed": "#cc0000",
    "kill_switch_engaged": "#cc0000",
    "maintenance_window_expiring": "#ffb700",
}


class SlackNotifier:

    def __init__(self) -> None:
        self._webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")

    def send(self, event: str, **kwargs) -> bool:
        color = _COLORS.get(event, "#aaaaaa")
        title = self._event_title(event)
        text = self._build_text(event, kwargs)

        payload = {
            "attachments": [{
                "color": color,
                "title": title,
                "text": text,
                "footer": f"MOP AI Platform | exec={kwargs.get('execution_id', '?')}",
            }]
        }

        if not self._webhook_url:
            logger.info("[NOTIFICATION_DRY_RUN] Slack %s: %s", event, text[:120])
            return False

        try:
            import urllib.request
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self._webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                ok = resp.status == 200
            logger.debug("Slack notification sent: event=%s status=%s", event, "ok" if ok else "fail")
            return ok
        except Exception as exc:
            logger.warning("Slack notification failed: %s", exc)
            return False

    def _event_title(self, event: str) -> str:
        return {
            "execution_started": "MOP Execution Started",
            "execution_passed": "MOP Execution Passed",
            "execution_failed": "MOP Execution Failed",
            "approval_required": "Approval Required",
            "step_failed_with_retry": "Step Failed (retrying)",
            "step_failed_no_retry": "Step Failed (no retry)",
            "rollback_started": "Rollback Started",
            "rollback_passed": "Rollback Completed",
            "rollback_failed": "Rollback Failed",
            "kill_switch_engaged": "KILL SWITCH ENGAGED",
            "maintenance_window_expiring": "Maintenance Window Expiring",
        }.get(event, event.replace("_", " ").title())

    def _build_text(self, event: str, ctx: dict) -> str:
        parts = []
        if "title" in ctx:
            parts.append(f"*{ctx['title']}*")
        if "execution_id" in ctx:
            parts.append(f"Execution: `{ctx['execution_id']}`")
        if "step_id" in ctx:
            parts.append(f"Step: `{ctx['step_id']}`")
        if "device" in ctx:
            parts.append(f"Device: `{ctx['device']}`")
        if "error" in ctx and ctx["error"]:
            parts.append(f"Error: {str(ctx['error'])[:200]}")
        if "duration_s" in ctx:
            parts.append(f"Duration: {ctx['duration_s']:.1f}s")
        if "reasons" in ctx:
            parts.append("Reasons: " + "; ".join(ctx["reasons"][:3]))
        if "scope" in ctx:
            parts.append(f"Scope: {ctx['scope']}")
        if "reason" in ctx:
            parts.append(f"Reason: {ctx['reason']}")
        return "\n".join(parts)
