"""
Email Notifier — sends HTML emails via SMTP.

Enable: set SMTP_HOST, SMTP_USER, SMTP_PASS, NOTIFY_EMAIL_TO env vars.
Dry-run: if env vars missing, logs with [NOTIFICATION_DRY_RUN] prefix.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

_IMPORTANT_EVENTS = {
    "execution_failed", "rollback_failed", "kill_switch_engaged",
    "execution_passed", "approval_required",
}


class EmailNotifier:

    def __init__(self) -> None:
        self._host = os.environ.get("SMTP_HOST", "")
        self._user = os.environ.get("SMTP_USER", "")
        self._pass = os.environ.get("SMTP_PASS", "")
        self._to   = os.environ.get("NOTIFY_EMAIL_TO", "")
        self._port = int(os.environ.get("SMTP_PORT", "587"))

    def send(self, event: str, **kwargs) -> bool:
        # Only email for important events
        if event not in _IMPORTANT_EVENTS:
            return False

        subject = self._build_subject(event, kwargs)
        body    = self._build_html(event, kwargs)

        if not all([self._host, self._user, self._pass, self._to]):
            logger.info("[NOTIFICATION_DRY_RUN] Email %s: %s", event, subject)
            return False

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = self._user
            msg["To"]      = self._to
            msg.attach(MIMEText(body, "html"))

            with smtplib.SMTP(self._host, self._port, timeout=10) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.login(self._user, self._pass)
                smtp.sendmail(self._user, self._to, msg.as_string())
            logger.debug("Email sent: event=%s to=%s", event, self._to)
            return True
        except Exception as exc:
            logger.warning("Email notification failed: %s", exc)
            return False

    def _build_subject(self, event: str, ctx: dict) -> str:
        title = ctx.get("title", ctx.get("execution_id", ""))
        prefixes = {
            "execution_passed": "[PASSED]",
            "execution_failed": "[FAILED]",
            "rollback_failed": "[ROLLBACK FAILED]",
            "kill_switch_engaged": "[KILL SWITCH]",
            "approval_required": "[APPROVAL REQUIRED]",
        }
        prefix = prefixes.get(event, f"[{event.upper()}]")
        return f"MOP Platform {prefix} {title}"

    def _build_html(self, event: str, ctx: dict) -> str:
        rows = ""
        for k, v in ctx.items():
            rows += f"<tr><td><b>{k}</b></td><td>{str(v)[:300]}</td></tr>"
        return f"""
        <html><body>
        <h2>MOP AI Platform — {event.replace('_', ' ').title()}</h2>
        <table border='1' cellpadding='4' style='border-collapse:collapse'>
        {rows}
        </table>
        </body></html>
        """
