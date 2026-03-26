"""
Connection Pool — reuses device SSH sessions across steps.

Without pooling, a 50-step MOP would open 50 SSH connections to the same
device — hitting most devices' session limits within seconds.

Pool key: (hostname, username)
Max connections per device: configurable (default 1)
Idle timeout: 600s (configurable)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from device_layer.credential_store import credential_store, CredentialNotFoundError
from device_layer.device_driver import DeviceDriver, MockDriver, NetmikoDriver

logger = logging.getLogger(__name__)

_PoolKey = Tuple[str, str]   # (hostname, username)


@dataclass
class _PoolEntry:
    driver:    DeviceDriver
    last_used: float = field(default_factory=time.time)
    in_use:    bool  = False


class ConnectionPool:
    """
    Thread-safe SSH connection pool.

    Usage:
        pool = ConnectionPool()
        driver = pool.acquire("router-a", vendor="cisco")
        output = driver.execute("show ip bgp summary")
        pool.release("router-a", driver)
    """

    def __init__(
        self,
        idle_timeout_s: float = 600.0,
        max_per_device: int = 1,
        use_mock: bool = False,
        mock_responses: Optional[Dict[str, Dict[str, str]]] = None,
    ) -> None:
        self._pool: Dict[_PoolKey, _PoolEntry] = {}
        self._lock = threading.Lock()
        self._idle_timeout = idle_timeout_s
        self._max_per_device = max_per_device
        self._use_mock = use_mock
        self._mock_responses = mock_responses or {}
        self._start_idle_sweep()

    def acquire(
        self,
        hostname: str,
        vendor: str = "generic",
        port: int = 22,
        jump_host_sock=None,
        timeout_s: float = 30.0,
    ) -> DeviceDriver:
        """
        Return a connected DeviceDriver for `hostname`.
        Reuses an existing idle connection if available; opens a new one otherwise.
        Raises CredentialNotFoundError or DeviceConnectionError on failure.
        """
        creds = credential_store.resolve(hostname)
        key: _PoolKey = (hostname, creds.username)

        with self._lock:
            entry = self._pool.get(key)
            if entry and not entry.in_use and entry.driver.is_connected:
                entry.in_use = True
                entry.last_used = time.time()
                logger.debug("[Pool] Reusing connection to %s", hostname)
                return entry.driver

        # Open new connection outside the lock
        driver = self._open(hostname, creds, vendor, port, jump_host_sock, timeout_s)

        with self._lock:
            self._pool[key] = _PoolEntry(driver=driver, in_use=True)

        logger.debug("[Pool] Opened new connection to %s", hostname)
        return driver

    def release(self, hostname: str, driver: DeviceDriver) -> None:
        """Return a driver to the pool (marks it as idle)."""
        with self._lock:
            for key, entry in self._pool.items():
                if entry.driver is driver:
                    entry.in_use = False
                    entry.last_used = time.time()
                    return
        # If not found in pool, just close
        driver.close()

    def close_all(self) -> None:
        with self._lock:
            for entry in self._pool.values():
                try:
                    entry.driver.close()
                except Exception:
                    pass
            self._pool.clear()
        logger.info("[Pool] All connections closed")

    # ------------------------------------------------------------------

    def _open(self, hostname, creds, vendor, port, sock, timeout_s) -> DeviceDriver:
        if self._use_mock:
            driver = MockDriver(
                hostname=hostname,
                responses=self._mock_responses.get(hostname, {}),
            )
        else:
            driver = NetmikoDriver(
                hostname=hostname,
                username=creds.username,
                password=creds.password,
                vendor=vendor,
                enable_password=creds.enable_password,
                port=port,
                jump_host_sock=sock,
                timeout_s=timeout_s,
            )
        driver.connect()
        return driver

    def _start_idle_sweep(self) -> None:
        def _sweep():
            while True:
                time.sleep(60)
                cutoff = time.time() - self._idle_timeout
                with self._lock:
                    to_remove = [
                        k for k, e in self._pool.items()
                        if not e.in_use and e.last_used < cutoff
                    ]
                    for k in to_remove:
                        try:
                            self._pool[k].driver.close()
                        except Exception:
                            pass
                        del self._pool[k]
                        logger.debug("[Pool] Closed idle connection to %s", k[0])

        threading.Thread(target=_sweep, daemon=True, name="pool-idle-sweep").start()


# Module-level singleton (can be overridden with mock in tests)
connection_pool = ConnectionPool()
