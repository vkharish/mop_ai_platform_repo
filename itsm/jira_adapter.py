"""
Jira Adapter — adds comments and transitions Jira issues.

Auth: ITSM_USERNAME + ITSM_TOKEN env vars (Basic auth with API token).
Dry-run: if env vars missing, logs with [ITSM_DRY_RUN] prefix.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)


class JiraAdapter:

    def __init__(self) -> None:
        self._user  = os.environ.get("ITSM_USERNAME", "")
        self._token = os.environ.get("ITSM_TOKEN", "")

    def add_comment(self, ticket_id: str, webhook_url: str, text: str) -> bool:
        if not all([self._user, self._token, webhook_url]):
            logger.info("[ITSM_DRY_RUN] Jira comment on %s: %s", ticket_id, text[:80])
            return False

        # Jira REST API v3: POST /rest/api/3/issue/{issueIdOrKey}/comment
        url = f"{webhook_url}/rest/api/3/issue/{ticket_id}/comment"
        payload = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph",
                              "content": [{"text": text, "type": "text"}]}],
            }
        }
        return self._request("POST", url, payload)

    def transition(self, ticket_id: str, webhook_url: str, state: str) -> bool:
        # Jira transitions require first fetching the transition_id
        # For simplicity, log dry-run unless webhook_url provided
        if not all([self._user, self._token, webhook_url]):
            logger.info("[ITSM_DRY_RUN] Jira transition %s → %s", ticket_id, state)
            return False

        # Fetch available transitions
        trans_url = f"{webhook_url}/rest/api/3/issue/{ticket_id}/transitions"
        try:
            req = urllib.request.Request(
                trans_url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Basic {self._auth()}",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            transitions = {t["name"]: t["id"] for t in data.get("transitions", [])}
            transition_id = transitions.get(state)
            if not transition_id:
                logger.warning("Jira transition '%s' not found for %s", state, ticket_id)
                return False

            payload = {"transition": {"id": transition_id}}
            post_url = f"{webhook_url}/rest/api/3/issue/{ticket_id}/transitions"
            return self._request("POST", post_url, payload)
        except Exception as exc:
            logger.warning("Jira transition failed: %s", exc)
            return False

    def _auth(self) -> str:
        return base64.b64encode(f"{self._user}:{self._token}".encode()).decode()

    def _request(self, method: str, url: str, payload: dict) -> bool:
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, method=method,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Authorization": f"Basic {self._auth()}",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                ok = resp.status in (200, 201, 204)
            logger.debug("Jira %s %s → %s", method, url, "ok" if ok else "fail")
            return ok
        except Exception as exc:
            logger.warning("Jira request failed: %s", exc)
            return False
