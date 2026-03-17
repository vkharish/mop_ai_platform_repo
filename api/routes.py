"""
MOP AI Platform — API routes

POST /api/v1/process                     Upload document → job_id (async)
GET  /api/v1/status/{job_id}             Poll job status
GET  /api/v1/result/{job_id}             Full result + download links
GET  /api/v1/download/{job_id}/{artifact} Download artifact file
GET  /api/v1/jobs                         List recent jobs
GET  /api/v1/logs/{job_id}               Per-job pipeline log (for debugging)
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse

from api.auth import verify_api_key
from api import job_store
from api.logging_config import JobLogger

router = APIRouter()
logger = logging.getLogger("api.routes")

_ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".text", ".md"}

# Map download slug → (result_outputs_key, media_type)
_ARTIFACT_MAP = {
    "zephyr":    ("zephyr_csv",      "text/csv"),
    "robot":     ("robot_framework", "text/plain"),
    "cli_rules": ("cli_rules",       "application/json"),
    "canonical": ("canonical_json",  "application/json"),
}


# ── POST /process ─────────────────────────────────────────────────────────────

@router.post("/process", summary="Upload a MOP document and start processing")
async def process_document(
    request: Request,
    file: UploadFile = File(..., description="PDF, DOCX, or TXT/MD file"),
    title: Optional[str] = Form(None, description="Override document title"),
    model: str = Form("claude-sonnet-4-6", description="Claude model ID"),
    skip_toon: bool = Form(False, description="Disable TOON token compression"),
    skip_guardrails: bool = Form(False, description="Skip post-LLM guardrail checks"),
    anthropic_api_key: Optional[str] = Form(
        None, description="Anthropic API key — overrides ANTHROPIC_API_KEY env var"
    ),
    _: None = Depends(verify_api_key),
):
    req_id = getattr(request.state, "request_id", "?")

    # Validate file type
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED_EXTENSIONS:
        logger.warning(
            f"[req:{req_id}] Rejected upload '{file.filename}' — unsupported extension '{suffix}'"
        )
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{suffix}'. Allowed: {', '.join(sorted(_ALLOWED_EXTENSIONS))}",
        )

    # Save upload to disk (keep extension so parsers can detect format)
    upload_dir = Path("output/uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{int(time.time() * 1000)}_{Path(file.filename).name}"
    tmp_path = upload_dir / safe_name
    with open(tmp_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    file_size_kb = tmp_path.stat().st_size // 1024

    # Create job record
    job_id = job_store.create_job(
        filename=file.filename,
        title=title,
        model=model,
        skip_toon=skip_toon,
        skip_guardrails=skip_guardrails,
    )

    logger.info(
        f"[req:{req_id}] Job created: job={job_id[:8]} file='{file.filename}' "
        f"size={file_size_kb}KB model={model} toon={not skip_toon} "
        f"guardrails={not skip_guardrails}"
    )

    # Submit to thread pool
    executor = request.app.state.executor
    loop = __import__("asyncio").get_event_loop()
    loop.run_in_executor(
        executor,
        _run_pipeline_sync,
        job_id,
        str(tmp_path),
        title,
        model,
        skip_toon,
        skip_guardrails,
        anthropic_api_key,
    )

    return {"job_id": job_id, "status": "pending", "filename": file.filename}


# ── Background worker ─────────────────────────────────────────────────────────

def _run_pipeline_sync(
    job_id: str,
    input_file: str,
    title: Optional[str],
    model: str,
    skip_toon: bool,
    skip_guardrails: bool,
    api_key: Optional[str],
) -> None:
    """
    Blocking pipeline execution — runs inside a ThreadPoolExecutor thread.

    Uses JobLogger to:
      - Write structured logs to the main log file (with [job:xxxx] prefix)
      - Persist per-job log lines into job_store["log"] for the /logs endpoint
      - Update job["progress_message"] at every major stage
    """
    jlog = JobLogger(job_id, job_store)
    start_ts = time.perf_counter()

    jlog.progress("Parsing document…")

    # Per-request Anthropic API key override
    _orig_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        os.environ["ANTHROPIC_API_KEY"] = api_key
        jlog.debug("Using per-request Anthropic API key")

    output_dir = Path("output") / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        # ── Stage 1: document ingestion ──────────────────────────────────────
        jlog.progress("Loading and parsing document…")
        from ingestion.document_loader import load as load_doc
        doc = load_doc(input_file)
        if title:
            doc.title = title
        jlog.info(
            f"Document loaded: title='{doc.title}' format={doc.source_format} "
            f"structure={doc.detected_structure} blocks={len(doc.blocks)} "
            f"~{len(doc.full_text):,} chars"
        )

        # ── Stage 2: grammar engine ──────────────────────────────────────────
        jlog.progress("Detecting CLI commands (grammar engine)…")
        from grammar_engine.cli_grammar import CLIGrammar
        grammar = CLIGrammar()
        pre_commands = grammar.extract_from_text(doc.full_text)
        pre_cmd_strings = [c.raw for c in pre_commands]
        jlog.info(f"Pre-LLM CLI commands detected: {len(pre_commands)}")

        # ── Stage 2b: TOON build ─────────────────────────────────────────────
        toon_doc = None
        if not skip_toon:
            jlog.progress("Building TOON compression…")
            try:
                from toon.builder import TOONBuilder
                toon_doc = TOONBuilder.build(doc, grammar)
                if toon_doc.toon_usable:
                    jlog.info(
                        f"TOON built: compression={toon_doc.compression_ratio:.1%} "
                        f"raw={toon_doc.estimated_raw_tokens:,}tok → "
                        f"toon={toon_doc.estimated_toon_tokens:,}tok "
                        f"sections={len(toon_doc.sections)}"
                    )
                else:
                    jlog.info(
                        f"TOON not applicable: {toon_doc.fallback_reason} — using raw text"
                    )
            except Exception as exc:
                jlog.warning(f"TOON build failed (non-fatal), falling back to raw text: {exc}")
                toon_doc = None
        else:
            jlog.info("TOON skipped (skip_toon=True)")

        # ── Stage 3: LLM extraction ──────────────────────────────────────────
        from ai_layer.context_chunker import ContextChunker
        chunker = ContextChunker()
        est_tokens = chunker.estimate_tokens(doc.full_text)
        toon_active = toon_doc is not None and toon_doc.toon_usable

        if toon_active:
            token_info = (
                f"{toon_doc.estimated_toon_tokens:,} TOON tokens "
                f"(was {est_tokens:,} raw)"
            )
            call_mode = "TOON"
        elif chunker.needs_chunking(doc):
            token_info = f"~{est_tokens:,} raw tokens (chunked)"
            call_mode = "chunked"
        else:
            token_info = f"~{est_tokens:,} raw tokens (single call)"
            call_mode = "single"

        jlog.progress(
            f"Running LLM extraction — {token_info}, mode={call_mode}, model={model}…"
        )
        jlog.info(f"LLM call: model={model} mode={call_mode} tokens≈{token_info}")

        from ai_layer.super_prompt_runner import SuperPromptRunner
        runner = SuperPromptRunner(
            model=model,
            api_key=api_key,
            use_toon=not skip_toon,
        )

        # Suppress pipeline's own print(json.dumps(...)) from polluting server logs
        _old_stdout, sys.stdout = sys.stdout, io.StringIO()
        try:
            from pipeline import run as pipeline_run
            result = pipeline_run(
                input_file=input_file,
                output_dir=str(output_dir),
                title=title,
                model=model,
                api_key=api_key,
                skip_guardrails=skip_guardrails,
                skip_toon=skip_toon,
            )
        finally:
            sys.stdout = _old_stdout

        # ── Log result details ───────────────────────────────────────────────
        elapsed = time.perf_counter() - start_ts
        meta = result.get("metadata", {})
        toon_res = result.get("toon", {})
        guardrail_res = meta.get("guardrails", {})

        jlog.info(
            f"Pipeline complete in {elapsed:.1f}s: "
            f"steps={result.get('total_steps', 0)} "
            f"commands={meta.get('total_commands', 0)} "
            f"chunks={meta.get('chunks_processed', 1)} "
            f"llm_attempts={meta.get('llm_attempt_count', 1)}"
        )

        if toon_res.get("used"):
            jlog.info(
                f"TOON savings: {toon_res['compression_ratio']} token reduction "
                f"({toon_res['raw_tokens']:,} → {toon_res['toon_tokens']:,} tokens)"
            )

        if guardrail_res:
            passed = guardrail_res.get("passed", True)
            coverage = guardrail_res.get("coverage_ratio", 1.0)
            jlog.info(
                f"Guardrails: {'PASSED' if passed else 'FAILED'} "
                f"coverage={coverage:.0%}"
            )
            for w in guardrail_res.get("warnings", []):
                jlog.warning(f"Guardrail warning: {w}")
            for e in guardrail_res.get("errors", []):
                jlog.error(f"Guardrail error: {e}")

        # Log output file paths + sizes
        for key, path_str in result.get("outputs", {}).items():
            p = Path(path_str)
            if p.exists():
                jlog.debug(f"Artifact: {key} → {p.name} ({p.stat().st_size // 1024}KB)")

        # Mark done
        job_store.update_job(
            job_id,
            status="done",
            progress_message=f"Complete — {result.get('total_steps', 0)} steps extracted in {elapsed:.1f}s",
            result=result,
        )

    except Exception as exc:
        elapsed = time.perf_counter() - start_ts
        # Full traceback into both the log file AND the per-job log
        tb_str = traceback.format_exc()
        jlog.error(
            f"Pipeline FAILED after {elapsed:.1f}s: {type(exc).__name__}: {exc}\n{tb_str}"
        )
        job_store.update_job(
            job_id,
            status="failed",
            progress_message=f"Failed: {type(exc).__name__}: {exc}",
            error=f"{type(exc).__name__}: {exc}",
            error_traceback=tb_str,
        )

    finally:
        # Restore API key env var
        if api_key:
            if _orig_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = _orig_key
            else:
                os.environ.pop("ANTHROPIC_API_KEY", None)

        # Delete the uploaded temp file
        try:
            Path(input_file).unlink(missing_ok=True)
            jlog.debug(f"Cleaned up upload: {Path(input_file).name}")
        except Exception as cleanup_err:
            jlog.warning(f"Failed to clean up upload file: {cleanup_err}")


# ── GET /status/{job_id} ──────────────────────────────────────────────────────

@router.get("/status/{job_id}", summary="Poll job status")
async def get_status(job_id: str, _: None = Depends(verify_api_key)):
    job = _get_job_or_404(job_id)
    return {
        "job_id":           job["job_id"],
        "status":           job["status"],
        "progress_message": job["progress_message"],
        "filename":         job["filename"],
        "model":            job["model"],
        "created_at":       job["created_at"],
        "updated_at":       job["updated_at"],
        "error":            job.get("error"),
    }


# ── GET /result/{job_id} ──────────────────────────────────────────────────────

@router.get("/result/{job_id}", summary="Get full result with download links")
async def get_result(job_id: str, _: None = Depends(verify_api_key)):
    job = _get_job_or_404(job_id)

    if job["status"] == "failed":
        raise HTTPException(
            status_code=422,
            detail={
                "message":   "Job failed",
                "error":     job.get("error", "Unknown error"),
                "traceback": job.get("error_traceback", ""),
            },
        )
    if job["status"] != "done":
        raise HTTPException(
            status_code=409,
            detail=f"Job not complete yet (status: {job['status']}). "
                   f"Poll /api/v1/status/{job_id}.",
        )

    result  = job.get("result", {})
    outputs = result.get("outputs", {})
    downloads = {
        slug: f"/api/v1/download/{job_id}/{slug}"
        for slug, (key, _) in _ARTIFACT_MAP.items()
        if outputs.get(key) and Path(outputs[key]).exists()
    }

    return {
        "job_id":    job_id,
        "status":    "done",
        "filename":  job["filename"],
        "result":    result,
        "downloads": downloads,
    }


# ── GET /download/{job_id}/{artifact} ─────────────────────────────────────────

@router.get(
    "/download/{job_id}/{artifact}",
    summary="Download a generated artifact",
    response_class=FileResponse,
)
async def download_artifact(
    job_id: str,
    artifact: str,
    _: None = Depends(verify_api_key),
):
    job = _get_job_or_404(job_id)

    if job["status"] != "done":
        raise HTTPException(status_code=409, detail="Job is not complete")

    if artifact not in _ARTIFACT_MAP:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown artifact '{artifact}'. Valid: {', '.join(_ARTIFACT_MAP)}",
        )

    output_key, media_type = _ARTIFACT_MAP[artifact]
    file_path = job.get("result", {}).get("outputs", {}).get(output_key)

    if not file_path or not Path(file_path).exists():
        logger.warning(
            f"Artifact missing on disk: job={job_id[:8]} artifact={artifact} "
            f"expected_path={file_path}"
        )
        raise HTTPException(status_code=404, detail="Artifact file not found on disk")

    logger.info(f"Download: job={job_id[:8]} artifact={artifact} file={Path(file_path).name}")
    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=Path(file_path).name,
    )


# ── GET /jobs ─────────────────────────────────────────────────────────────────

@router.get("/jobs", summary="List recent jobs (latest 20)")
async def list_jobs(_: None = Depends(verify_api_key)):
    return [
        {
            "job_id":     j["job_id"],
            "status":     j["status"],
            "filename":   j["filename"],
            "model":      j["model"],
            "skip_toon":  j["skip_toon"],
            "created_at": j["created_at"],
            "updated_at": j["updated_at"],
        }
        for j in job_store.list_jobs(limit=20)
    ]


# ── GET /logs/{job_id} ────────────────────────────────────────────────────────

@router.get(
    "/logs/{job_id}",
    summary="Get per-job pipeline log for debugging",
    response_class=PlainTextResponse,
)
async def get_job_logs(
    job_id: str,
    tail: int = 200,
    _: None = Depends(verify_api_key),
):
    """
    Returns the last `tail` log lines captured during this job's pipeline run.
    Each line is: HH:MM:SS.mmm LEVEL   message

    Useful for debugging without needing to grep the server log file.
    Pass ?tail=500 for more lines (max stored: 500).
    """
    job = _get_job_or_404(job_id)
    lines: list = job.get("log") or []
    if not lines:
        status = job["status"]
        if status == "pending":
            return "Job is still pending — no logs yet."
        return "No log captured for this job."

    tail = max(1, min(tail, 500))
    selected = lines[-tail:]
    header = (
        f"# Job: {job_id}\n"
        f"# File: {job['filename']}\n"
        f"# Status: {job['status']}\n"
        f"# Showing last {len(selected)} of {len(lines)} lines\n"
        f"# ─────────────────────────────────────────────────\n"
    )
    return header + "\n".join(selected)


# ── Helper ────────────────────────────────────────────────────────────────────

def _get_job_or_404(job_id: str) -> dict:
    job = job_store.get_job(job_id)
    if not job:
        logger.debug(f"Job not found: {job_id}")
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return job
