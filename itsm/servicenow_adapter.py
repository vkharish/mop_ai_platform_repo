"""
ServiceNow Adapter — adds comments and transitions incidents/change requests.

Auth: ITSM_USERNAME + ITSM_PASSWORD env vars (Basic auth).
Dry-run: if env vars missing, logs with [ITSM_DRY_RUN] prefix.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)


class ServiceNowAdapter:

    def __init__(self) -> None:
        self._user = os.environ.get("ITSM_USERNAME", "")
        self._pass = os.environ.get("ITSM_PASSWORD", "")

    def add_comment(self, ticket_id: str, webhook_url: str, text: str) -> bool:
        if not all([self._user, self._pass, webhook_url]):
            logger.info("[ITSM_DRY_RUN] ServiceNow comment on %s: %s", ticket_id, text[:80])
            return False

        # ServiceNow REST Table API: PATCH /api/now/table/change_request/{sys_id}
        url = webhook_url if webhook_url else f"/api/now/table/change_request/{ticket_id}"
        payload = {"work_notes": text}
        return self._request("PATCH", url, payload)

    def transition(self, ticket_id: str, webhook_url: str, state: str) -> bool:
        state_map = {
            "In Progress": "2",
            "Implemented": "3",
            "Failed": "-1",
            "Closed": "7",
        }
        if not all([self._user, self._pass, webhook_url]):
            logger.info("[ITSM_DRY_RUN] ServiceNow transition %s → %s", ticket_id, state)
            return False

        url = webhook_url if webhook_url else f"/api/now/table/change_request/{ticket_id}"
        payload = {"state": state_map.get(state, state)}
        return self._request("PATCH", url, payload)

    def _request(self, method: str, url: str, payload: dict) -> bool:
        try:
            data = json.dumps(payload).encode("utf-8")
            creds = base64.b64encode(f"{self._user}:{self._pass}".encode()).decode()
            req = urllib.request.Request(
                url, data=data, method=method,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Authorization": f"Basic {creds}",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                ok = resp.status in (200, 201, 204)
            logger.debug("ServiceNow %s %s → %s", method, url, "ok" if ok else "fail")
            return ok
        except Exception as exc:
            logger.warning("ServiceNow request failed: %s", exc)
            return False
