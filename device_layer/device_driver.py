"""
Device Driver — abstract base + concrete implementations.

DeviceDriver is the only place in the codebase that knows how to talk to
a real network device. All agents use drivers exclusively — no raw SSH elsewhere.

Implementations:
  MockDriver      — returns configurable canned responses (for testing / dry-run)
  NetmikoDriver   — real SSH via Netmiko (covers all 9 supported vendors)
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Netmiko device_type mapping from our vendor strings
_VENDOR_TO_DEVICE_TYPE: Dict[str, str] = {
    "cisco":     "cisco_xr",
    "juniper":   "juniper_junos",
    "nokia":     "nokia_sros",
    "arista":    "arista_eos",
    "huawei":    "huawei_vrp",
    "f5":        "linux",        # F5 tmsh via bash
    "palo_alto": "paloalto_panos",
    "checkpoint":"checkpoint_gaia",
    "generic":   "cisco_ios",    # best-effort fallback
}


class DeviceConnectionError(Exception):
    pass


class DeviceCommandError(Exception):
    pass


class DeviceDriver(ABC):
    """Abstract base for all device drivers."""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def execute(self, command: str, timeout_s: float = 30.0) -> str: ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()


@dataclass
class MockDriver(DeviceDriver):
    """
    Returns canned responses for testing and dry-run mode.

    Configure responses dict: {pattern: response_text}
    If no pattern matches, returns the default_response.
    """
    hostname:         str
    responses:        Dict[str, str] = field(default_factory=dict)
    default_response: str = "% OK"
    simulate_delay_s: float = 0.05
    _connected:       bool = field(default=False, init=False)

    def connect(self) -> None:
        time.sleep(self.simulate_delay_s)
        self._connected = True
        logger.debug("[MockDriver] Connected to %s", self.hostname)

    def execute(self, command: str, timeout_s: float = 30.0) -> str:
        if not self._connected:
            raise DeviceConnectionError("Not connected")
        time.sleep(self.simulate_delay_s)
        # Try pattern matching
        for pattern, response in self.responses.items():
            if re.search(pattern, command, re.IGNORECASE):
                logger.debug("[MockDriver:%s] CMD: %s → matched '%s'", self.hostname, command, pattern)
                return response
        logger.debug("[MockDriver:%s] CMD: %s → default", self.hostname, command)
        return self.default_response

    def close(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected


class NetmikoDriver(DeviceDriver):
    """
    Real SSH driver via Netmiko.

    Vendor is mapped to Netmiko device_type automatically from the
    CLICommand.vendor field detected in Phase 1.
    """

    def __init__(
        self,
        hostname: str,
        username: str,
        password: str,
        vendor: str = "generic",
        enable_password: str = "",
        port: int = 22,
        jump_host_sock=None,
        timeout_s: float = 30.0,
    ) -> None:
        self.hostname = hostname
        self._username = username
        self._password = password
        self._enable_password = enable_password
        self._vendor = vendor
        self._port = port
        self._sock = jump_host_sock
        self._timeout = timeout_s
        self._connection = None
        self._lock = threading.Lock()

    def connect(self) -> None:
        try:
            import netmiko  # type: ignore
        except ImportError:
            raise DeviceConnectionError(
                "netmiko not installed. Run: pip install netmiko"
            )
        device_type = _VENDOR_TO_DEVICE_TYPE.get(self._vendor, "cisco_ios")
        conn_params = {
            "device_type":   device_type,
            "host":          self.hostname,
            "username":      self._username,
            "password":      self._password,
            "secret":        self._enable_password,
            "port":          self._port,
            "timeout":       self._timeout,
            "session_log":   f"output/sessions/{self.hostname}_{int(time.time())}.log",
        }
        if self._sock:
            conn_params["sock"] = self._sock

        # Jump host / bastion support
        jump_host = os.environ.get("JUMP_HOST") or os.environ.get(
            f"JUMP_HOST_{self.hostname.upper().replace('.', '_')}"
        )
        if jump_host and self._sock is None:
            jump_user = os.environ.get("JUMP_USER", "")
            jump_pass = os.environ.get("JUMP_PASS", "")
            try:
                import paramiko
                transport = paramiko.Transport((jump_host, 22))
                transport.connect(username=jump_user, password=jump_pass)
                chan = transport.open_channel(
                    "direct-tcpip",
                    (self.hostname, self._port),
                    ("127.0.0.1", 0),
                )
                conn_params["sock"] = chan
                logger.info("Using jump host %s for %s", jump_host, self.hostname)
            except ImportError:
                logger.warning("paramiko not installed — jump host ignored")
            except Exception as exc:
                logger.warning("Jump host connection failed (%s): %s — trying direct", jump_host, exc)

        try:
            self._connection = netmiko.ConnectHandler(**conn_params)
            if self._enable_password:
                self._connection.enable()
            logger.info("Connected to %s (%s)", self.hostname, device_type)
        except Exception as exc:
            raise DeviceConnectionError(
                f"Failed to connect to {self.hostname}: {exc}"
            ) from exc

    def execute(self, command: str, timeout_s: float = 30.0) -> str:
        if not self.is_connected:
            raise DeviceConnectionError(f"Not connected to {self.hostname}")
        with self._lock:
            try:
                output = self._connection.send_command(
                    command,
                    read_timeout=timeout_s,
                    expect_string=None,
                )
                logger.debug("[%s] CMD: %s | output_len=%d", self.hostname, command[:60], len(output))
                return output
            except Exception as exc:
                raise DeviceCommandError(
                    f"Command failed on {self.hostname}: '{command}' — {exc}"
                ) from exc

    def close(self) -> None:
        if self._connection:
            try:
                self._connection.disconnect()
            except Exception:
                pass
            self._connection = None
        logger.debug("Disconnected from %s", self.hostname)

    @property
    def is_connected(self) -> bool:
        return self._connection is not None and self._connection.is_alive()
