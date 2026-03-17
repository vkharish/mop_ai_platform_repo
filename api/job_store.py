"""
File-based job store — survives server restarts.

Each job is a JSON file at output/jobs/{job_id}.json.
All writes are protected by a threading.Lock so concurrent
background threads can safely update the same job.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("api.job_store")

_JOBS_DIR = Path("output/jobs")
_lock = threading.Lock()


def _job_path(job_id: str) -> Path:
    return _JOBS_DIR / f"{job_id}.json"


def create_job(
    filename: str,
    title: Optional[str],
    model: str,
    skip_toon: bool,
    skip_guardrails: bool,
) -> str:
    """Create a new job record and return the job_id."""
    _JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    job: Dict[str, Any] = {
        "job_id": job_id,
        "status": "pending",          # pending | processing | done | failed
        "filename": filename,
        "title": title,
        "model": model,
        "skip_toon": skip_toon,
        "skip_guardrails": skip_guardrails,
        "created_at": now,
        "updated_at": now,
        "progress_message": "Queued",
        "result": None,
        "error": None,
        "output_dir": str(Path("output") / job_id),
    }
    with _lock:
        _job_path(job_id).write_text(json.dumps(job, indent=2))
    logger.info(f"Job created: {job_id[:8]} file='{filename}' model={model}")
    return job_id


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    p = _job_path(job_id)
    if not p.exists():
        return None
    with _lock:
        try:
            return json.loads(p.read_text())
        except Exception:
            return None


def update_job(job_id: str, **kwargs) -> None:
    with _lock:
        p = _job_path(job_id)
        if not p.exists():
            return
        try:
            job = json.loads(p.read_text())
        except Exception:
            return
        old_status = job.get("status")
        job.update(kwargs)
        job["updated_at"] = datetime.now(timezone.utc).isoformat()
        p.write_text(json.dumps(job, indent=2))

    # Log status transitions outside the lock
    new_status = kwargs.get("status")
    if new_status and new_status != old_status:
        level = logging.ERROR if new_status == "failed" else logging.INFO
        logger.log(level, f"Job {job_id[:8]}: {old_status} → {new_status}")


def list_jobs(limit: int = 20) -> List[Dict[str, Any]]:
    _JOBS_DIR.mkdir(parents=True, exist_ok=True)
    jobs = []
    paths = sorted(
        _JOBS_DIR.glob("*.json"),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )[:limit]
    for p in paths:
        with _lock:
            try:
                jobs.append(json.loads(p.read_text()))
            except Exception:
                pass
    return jobs
