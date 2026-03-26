"""
Concurrency Controller — enforces max_concurrent_devices limit.

Prevents more than N devices from having active SSH sessions simultaneously.
Uses a threading.BoundedSemaphore keyed by device hostname.

Usage:
    from execution_engine.concurrency_controller import concurrency_controller
    with concurrency_controller.acquire_device(hostname, timeout_s=300):
        driver.execute(...)
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CONCURRENT_DEVICES = 5
_DEFAULT_QUEUE_TIMEOUT_S = 300  # 5 minutes


class ConcurrencyController:
    """
    Global semaphore pool: at most max_concurrent_devices devices active at once.
    Per-device lock: at most 1 in-flight step per device.
    """

    def __init__(
        self,
        max_concurrent_devices: int = _DEFAULT_MAX_CONCURRENT_DEVICES,
        queue_timeout_s: float = _DEFAULT_QUEUE_TIMEOUT_S,
    ) -> None:
        self._max_devices = max_concurrent_devices
        self._timeout_s = queue_timeout_s
        # Global semaphore: limits total concurrent device connections
        self._global_sem = threading.BoundedSemaphore(max_concurrent_devices)
        # Per-device locks: prevents concurrent commands on same device
        self._device_locks: Dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()
        logger.info("ConcurrencyController: max_concurrent_devices=%d, timeout_s=%.0f",
                    max_concurrent_devices, queue_timeout_s)

    def reconfigure(self, max_concurrent_devices: int) -> None:
        """Update the limit (takes effect on next acquire)."""
        self._max_devices = max_concurrent_devices
        self._global_sem = threading.BoundedSemaphore(max_concurrent_devices)

    @contextmanager
    def acquire_device(self, hostname: str, timeout_s: Optional[float] = None):
        """
        Context manager that acquires both the global slot and per-device lock.

        Raises RuntimeError if timeout exceeded (FIFO starvation protection).
        """
        timeout = timeout_s if timeout_s is not None else self._timeout_s
        tag = f"[{hostname}]"

        # Acquire global slot
        logger.debug("%s Waiting for global concurrency slot (max=%d)", tag, self._max_devices)
        acquired = self._global_sem.acquire(timeout=timeout)
        if not acquired:
            raise RuntimeError(
                f"Concurrency timeout: could not acquire device slot for '{hostname}' "
                f"within {timeout:.0f}s. Max concurrent devices={self._max_devices}"
            )
        logger.debug("%s Global slot acquired", tag)

        # Acquire per-device lock
        lock = self._get_device_lock(hostname)
        lock_acquired = lock.acquire(timeout=timeout)
        if not lock_acquired:
            self._global_sem.release()
            raise RuntimeError(
                f"Concurrency timeout: per-device lock for '{hostname}' "
                f"held for >{timeout:.0f}s"
            )
        logger.debug("%s Per-device lock acquired", tag)

        try:
            yield
        finally:
            lock.release()
            self._global_sem.release()
            logger.debug("%s Concurrency slots released", tag)

    def active_device_count(self) -> int:
        """Approximate number of currently active device connections."""
        # BoundedSemaphore doesn't expose count directly — estimate from _value
        try:
            return self._max_devices - self._global_sem._value
        except AttributeError:
            return 0

    def _get_device_lock(self, hostname: str) -> threading.Lock:
        with self._meta_lock:
            if hostname not in self._device_locks:
                self._device_locks[hostname] = threading.Lock()
            return self._device_locks[hostname]


# Module-level singleton — loaded from execution_defaults.yaml on first use
def _load_max_from_config() -> int:
    try:
        import yaml
        from pathlib import Path
        cfg_path = Path("configs") / "execution_defaults.yaml"
        if cfg_path.exists():
            with open(cfg_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            return int(cfg.get("max_concurrent_devices", _DEFAULT_MAX_CONCURRENT_DEVICES))
    except Exception:
        pass
    return _DEFAULT_MAX_CONCURRENT_DEVICES


concurrency_controller = ConcurrencyController(max_concurrent_devices=_load_max_from_config())
