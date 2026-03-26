"""
MOP AI Platform — FastAPI Application

Start locally:
    uvicorn api.main:app --reload --port 8000

With Docker:
    docker compose up

UI:       http://localhost:8000
API docs: http://localhost:8000/docs
"""

from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from api.logging_config import configure_logging
from api.routes import router
from api.execution_routes import router as execution_router

import logging
logger = logging.getLogger("api.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Logging must be first ──
    configure_logging(
        log_dir=os.getenv("LOG_DIR", "logs"),
        level=os.getenv("LOG_LEVEL", "INFO"),
    )
    logger.info("MOP AI Platform starting up")

    # ── Output directories ──
    for d in ("output/jobs", "output/uploads", "logs"):
        Path(d).mkdir(parents=True, exist_ok=True)

    # ── Thread pool for blocking pipeline.run() calls ──
    workers = int(os.getenv("MAX_WORKERS", "3"))
    executor = ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="mop-worker",
    )
    app.state.executor = executor
    logger.info(f"ThreadPoolExecutor ready — max_workers={workers}")

    yield

    logger.info("MOP AI Platform shutting down")
    executor.shutdown(wait=False)


app = FastAPI(
    title="MOP AI Platform",
    description=(
        "Convert MOP/SOP procedural documents (PDF, DOCX, TXT) into:\n"
        "- **Zephyr Scale** CSV bulk import test cases\n"
        "- **Robot Framework** `.robot` automation tests\n"
        "- **CLI validation rules** JSON\n\n"
        "Upload via the web UI at `/` or call the REST API directly."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ── Middleware: log every request ─────────────────────────────────────────────

@app.middleware("http")
async def request_logging_middleware(request: Request, call_next) -> Response:
    request_id = str(uuid.uuid4())[:8]
    request.state.request_id = request_id
    start = time.perf_counter()

    logger.debug(
        f"[req:{request_id}] → {request.method} {request.url.path} "
        f"client={request.client.host if request.client else 'unknown'}"
    )

    try:
        response: Response = await call_next(request)
    except Exception as exc:
        logger.error(
            f"[req:{request_id}] ✗ UNHANDLED {request.method} {request.url.path} "
            f"error={exc!r}",
            exc_info=True,
        )
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    elapsed_ms = (time.perf_counter() - start) * 1000
    level = logging.WARNING if response.status_code >= 400 else logging.INFO
    logger.log(
        level,
        f"[req:{request_id}] ← {response.status_code} "
        f"{request.method} {request.url.path} "
        f"{elapsed_ms:.1f}ms",
    )
    response.headers["X-Request-ID"] = request_id
    return response


# ── CORS ──────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Routes ────────────────────────────────────────────────────────────────────

app.include_router(router, prefix="/api/v1", tags=["MOP Processing"])
app.include_router(execution_router)


# ── Static files + UI ─────────────────────────────────────────────────────────

_static_dir = Path("static")
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", include_in_schema=False)
async def serve_ui():
    index = Path("static/index.html")
    if index.exists():
        return FileResponse(str(index))
    return {"message": "MOP AI Platform API", "docs": "/docs"}


@app.get("/health", tags=["Health"])
async def health():
    from execution_engine.kill_switch import kill_switch
    return {"status": "ok", "version": "1.0.0", "kill_switch": kill_switch.is_set()}
