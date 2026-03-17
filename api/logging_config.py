"""
Centralised logging configuration for the MOP AI Platform API.

Sets up:
  - Console handler  — INFO+ with colour-coded levels
  - Rotating file    — DEBUG+ at logs/mop_api.log (10 MB × 5 backups)
  - Per-job capture  — each background worker appends to job["log"] list
                       so you can call GET /api/v1/logs/{job_id} to debug
                       a specific run without grepping through the main log

Usage (call once at app startup):
    from api.logging_config import configure_logging
    configure_logging()

Per-job logger:
    from api.logging_config import JobLogger
    jlog = JobLogger(job_id, job_store)
    jlog.info("Stage started")
    jlog.error("Something broke", exc_info=True)   # captures full traceback
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import traceback
from pathlib import Path
from typing import Optional


# ── Log format ────────────────────────────────────────────────────────────────

_FILE_FORMAT = (
    "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)-24s | %(message)s"
)
_CONSOLE_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# ANSI colour codes for console (skipped on Windows / non-TTY)
_COLOURS = {
    "DEBUG":    "\033[36m",   # cyan
    "INFO":     "\033[32m",   # green
    "WARNING":  "\033[33m",   # yellow
    "ERROR":    "\033[31m",   # red
    "CRITICAL": "\033[35m",   # magenta
}
_RESET = "\033[0m"


class _ColouredFormatter(logging.Formatter):
    def __init__(self, fmt: str, datefmt: str, use_colour: bool = True):
        super().__init__(fmt, datefmt=datefmt)
        self._use_colour = use_colour

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if self._use_colour and record.levelname in _COLOURS:
            return f"{_COLOURS[record.levelname]}{msg}{_RESET}"
        return msg


# ── Public setup ──────────────────────────────────────────────────────────────

def configure_logging(log_dir: str = "logs", level: str = "INFO") -> None:
    """
    Configure root logger with console + rotating-file handlers.
    Safe to call multiple times (handlers are added only once).
    """
    root = logging.getLogger()

    # Avoid adding duplicate handlers on reload (uvicorn --reload)
    if getattr(root, "_mop_configured", False):
        return

    root.setLevel(logging.DEBUG)  # handlers filter independently

    # ── Console ──
    console_level = getattr(logging, level.upper(), logging.INFO)
    use_colour = sys.stdout.isatty()
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(
        _ColouredFormatter(_CONSOLE_FORMAT, _DATE_FORMAT, use_colour=use_colour)
    )
    root.addHandler(console_handler)

    # ── Rotating file ──
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_path / "mop_api.log",
        maxBytes=10 * 1024 * 1024,   # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(file_handler)

    # Quieten noisy third-party loggers
    for noisy in ("httpcore", "httpx", "anthropic._base_client", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    root._mop_configured = True  # type: ignore[attr-defined]

    logging.getLogger("api").info(
        f"Logging initialised — console:{level.upper()} file:DEBUG → {log_path}/mop_api.log"
    )


# ── Per-job logger ────────────────────────────────────────────────────────────

class JobLogger:
    """
    Thin wrapper around the standard logger that:
      1. Prefixes every message with [job:{short_id}] for easy grep
      2. Appends every log line to job_store["log"] so it's retrievable
         via GET /api/v1/logs/{job_id} without searching log files
      3. Captures full tracebacks on error/exception calls
    """

    def __init__(self, job_id: str, job_store_module) -> None:
        self._job_id   = job_id
        self._short    = job_id[:8]
        self._store    = job_store_module
        self._logger   = logging.getLogger("api.worker")
        # Initialise the log list in the job record
        self._store.update_job(job_id, log=[])

    # ── Logging methods ───────────────────────────────────────────────────────

    def debug(self, msg: str) -> None:
        self._emit(logging.DEBUG, msg)

    def info(self, msg: str) -> None:
        self._emit(logging.INFO, msg)

    def warning(self, msg: str) -> None:
        self._emit(logging.WARNING, msg)

    def error(self, msg: str, exc_info: bool = False) -> None:
        if exc_info:
            tb = traceback.format_exc()
            if tb and tb.strip() != "NoneType: None":
                msg = f"{msg}\n{tb}"
        self._emit(logging.ERROR, msg)

    def exception(self, msg: str) -> None:
        """Log ERROR with current exception traceback attached."""
        self.error(msg, exc_info=True)

    # ── Progress helper (also updates job store progress_message) ─────────────

    def progress(self, message: str) -> None:
        """Update both the log and the job's progress_message field."""
        self.info(message)
        self._store.update_job(self._job_id, progress_message=message)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _emit(self, level: int, msg: str) -> None:
        prefixed = f"[job:{self._short}] {msg}"

        # Standard logger (goes to console + file)
        self._logger.log(level, prefixed)

        # Append to per-job log stored in job record
        level_name = logging.getLevelName(level)
        import datetime
        line = f"{datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]} {level_name:<8} {msg}"
        try:
            job = self._store.get_job(self._job_id)
            if job is not None:
                log_list = job.get("log") or []
                log_list.append(line)
                # Keep last 500 lines to avoid unbounded growth
                if len(log_list) > 500:
                    log_list = log_list[-500:]
                self._store.update_job(self._job_id, log=log_list)
        except Exception:
            pass  # Never let logging break the pipeline
