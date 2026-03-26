"""
Credential Store — resolves device credentials at runtime.

Resolution order (first found wins):
  1. HashiCorp Vault HTTP API (if VAULT_ADDR + VAULT_TOKEN env vars set)
  2. Environment variables  DEVICE_CREDS_{HOSTNAME}_USER / _PASS / _ENABLE
  3. Local encrypted file   .credentials.json  (AES-256-GCM, key from CREDS_KEY env var)

Rules:
  - Credentials NEVER written to canonical model or API responses
  - Password NEVER logged (only username + source)
  - CredentialStore raises CredentialNotFoundError (not returns None) so callers
    cannot accidentally proceed without credentials
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CREDS_FILE = Path(".credentials.json")


class CredentialNotFoundError(Exception):
    pass


@dataclass(frozen=True)
class Credentials:
    username:        str
    password:        str
    enable_password: str = ""
    source:          str = "unknown"   # vault | env | file


class CredentialStore:
    """
    Resolves device credentials without ever storing secrets in memory
    beyond the duration of a single resolve() call.
    """

    def resolve(self, hostname: str) -> Credentials:
        """
        Resolve credentials for `hostname`. Raises CredentialNotFoundError if
        no credentials found from any source.
        """
        # Normalise hostname for env var lookup (uppercase, replace . and - with _)
        host_key = hostname.upper().replace(".", "_").replace("-", "_")

        # 1. Vault
        creds = self._try_vault(hostname)
        if creds:
            logger.info("Credentials for %s resolved from Vault", hostname)
            return creds

        # 2. Environment variables
        user = os.environ.get(f"DEVICE_CREDS_{host_key}_USER")
        pw   = os.environ.get(f"DEVICE_CREDS_{host_key}_PASS", "")
        en   = os.environ.get(f"DEVICE_CREDS_{host_key}_ENABLE", "")
        if user:
            logger.info("Credentials for %s resolved from environment variables", hostname)
            return Credentials(username=user, password=pw, enable_password=en, source="env")

        # 3. Fallback: global env creds (DEVICE_DEFAULT_USER / _PASS)
        user = os.environ.get("DEVICE_DEFAULT_USER")
        pw   = os.environ.get("DEVICE_DEFAULT_PASS", "")
        en   = os.environ.get("DEVICE_DEFAULT_ENABLE", "")
        if user:
            logger.info("Credentials for %s resolved from default env vars", hostname)
            return Credentials(username=user, password=pw, enable_password=en, source="env_default")

        # 4. Encrypted local file
        creds = self._try_file(hostname)
        if creds:
            logger.info("Credentials for %s resolved from encrypted file", hostname)
            return creds

        raise CredentialNotFoundError(
            f"No credentials found for '{hostname}'. "
            "Set DEVICE_CREDS_{HOST}_USER/_PASS env vars or configure Vault."
        )

    # ------------------------------------------------------------------
    # Private resolvers
    # ------------------------------------------------------------------

    def _try_vault(self, hostname: str) -> Optional[Credentials]:
        vault_addr  = os.environ.get("VAULT_ADDR")
        vault_token = os.environ.get("VAULT_TOKEN")
        if not vault_addr or not vault_token:
            return None
        try:
            import hvac  # type: ignore
            client = hvac.Client(url=vault_addr, token=vault_token)
            path = f"network/{hostname}"
            secret = client.secrets.kv.read_secret_version(path=path)
            data = secret["data"]["data"]
            return Credentials(
                username=data["username"],
                password=data["password"],
                enable_password=data.get("enable_password", ""),
                source="vault",
            )
        except ImportError:
            logger.debug("hvac not installed — Vault lookup skipped")
        except Exception as exc:
            logger.debug("Vault lookup failed for %s: %s", hostname, exc)
        return None

    def _try_file(self, hostname: str) -> Optional[Credentials]:
        if not _CREDS_FILE.exists():
            return None
        key_b64 = os.environ.get("CREDS_KEY")
        if not key_b64:
            logger.debug("CREDS_KEY not set — encrypted credentials file skipped")
            return None
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
            key = base64.b64decode(key_b64)
            raw = _CREDS_FILE.read_bytes()
            nonce, ct = raw[:12], raw[12:]
            plaintext = AESGCM(key).decrypt(nonce, ct, None)
            data: dict = json.loads(plaintext)
            host_data = data.get(hostname) or data.get(hostname.lower())
            if not host_data:
                return None
            return Credentials(
                username=host_data["username"],
                password=host_data["password"],
                enable_password=host_data.get("enable_password", ""),
                source="file",
            )
        except Exception as exc:
            logger.debug("Credential file lookup failed for %s: %s", hostname, exc)
        return None


# Module-level singleton
credential_store = CredentialStore()
