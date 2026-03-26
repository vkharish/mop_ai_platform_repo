"""
Kill Switch — emergency halt mechanism for all active executions.

Two layers:
  1. In-process threading.Event  (fast, ~0ms — checked before every command)
  2. File sentinel at output/kill_switch.flag  (survives process restart)

Usage:
    from execution_engine.kill_switch import kill_switch
    kill_switch.engage()       # halt everything
    kill_switch.clear()        # allow new executions
    kill_switch.is_set()       # check before dispatching a command
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_FLAG_FILE = Path("output") / "kill_switch.flag"


class KillSwitch:
    def __init__(self) -> None:
        self._event = threading.Event()
        # Restore state from flag file if present (process restart scenario)
        if _FLAG_FILE.exists():
            self._event.set()
            logger.warning("Kill switch flag file found on startup — kill switch is ENGAGED")

    def engage(self, reason: str = "manual") -> None:
        self._event.set()
        _FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _FLAG_FILE.write_text(reason)
        logger.critical("KILL SWITCH ENGAGED — reason: %s", reason)

    def clear(self) -> None:
        self._event.clear()
        if _FLAG_FILE.exists():
            _FLAG_FILE.unlink()
        logger.warning("Kill switch cleared — executions may resume")

    def is_set(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        if _FLAG_FILE.exists():
            return _FLAG_FILE.read_text().strip()
        return ""


# Module-level singleton — import this everywhere
kill_switch = KillSwitch()
