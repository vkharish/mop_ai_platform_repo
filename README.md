# MOP AI Platform

Convert any procedural document (MOP, SOP, runbook) into structured test artifacts — automatically, using Claude AI.

**Outputs:**
- Zephyr Scale CSV bulk import test cases
- Robot Framework `.robot` automation tests
- CLI validation rules JSON

**Supports:** PDF · DOCX · TXT · Markdown
**Vendors:** Cisco · Juniper · Nokia · Arista · F5 · Palo Alto · Check Point · Huawei · Ericsson

---

## Quick Start

### Prerequisites

| Requirement | Version |
|------------|---------|
| Python | 3.10+ |
| pip | latest |
| Anthropic API key | [console.anthropic.com](https://console.anthropic.com) |

---

## Option 1 — Web UI + REST API (recommended)

The easiest way. Upload documents via browser, download artifacts, view live processing logs.

### Step 1 — Clone and set up environment

```bash
git clone https://github.com/vkharish/mop_ai_platform_repo.git
cd mop_ai_platform_repo

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Step 2 — Configure API key

```bash
cp .env.example .env
```

Open `.env` and fill in your Anthropic API key:

```env
ANTHROPIC_API_KEY=sk-ant-your-key-here
MOP_API_KEY=                        # leave blank = no auth required (dev mode)
MAX_WORKERS=3
LOG_LEVEL=INFO
```

### Step 3 — Start the server

```bash
uvicorn api.main:app --reload --port 8000
```

### Step 4 — Open the UI

```
http://localhost:8000
```

Drag in a PDF/DOCX/TXT MOP → click **Process Document** → download Zephyr CSV, Robot test, and CLI rules.

**API docs (Swagger UI):** `http://localhost:8000/docs`

---

## Option 2 — Docker (zero Python setup)

### Step 1 — Clone and configure

```bash
git clone https://github.com/vkharish/mop_ai_platform_repo.git
cd mop_ai_platform_repo

cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY=sk-ant-your-key-here
```

### Step 2 — Build and run

```bash
docker compose up --build
```

### Step 3 — Open the UI

```
http://localhost:8000
```

Generated artifacts are saved to `./output/` on your host machine (volume-mounted into the container).

To stop:
```bash
docker compose down
```

---

## Option 3 — Command Line (no server)

Process a single document directly from the terminal.

```bash
# Basic usage
python pipeline.py --input mop.pdf --output ./output

# With options
python pipeline.py \
  --input mop.pdf \
  --output ./output \
  --title "BGP Migration MOP" \
  --model claude-sonnet-4-6 \
  --skip-toon              # disable token compression (slower, more tokens)
  --skip-guardrails        # skip post-LLM quality checks

# DOCX
python pipeline.py --input runbook.docx --output ./output

# Plain text / Markdown
python pipeline.py --input procedure.txt --output ./output
```

**Output files written to `./output/`:**
```
BGP_Migration_MOP_zephyr.csv       ← Zephyr Scale bulk import
BGP_Migration_MOP.robot            ← Robot Framework test suite
BGP_Migration_MOP_cli_rules.json   ← CLI validation rules
BGP_Migration_MOP_canonical.json   ← Full structured data (debug)
```

**The API key can also be passed directly:**
```bash
python pipeline.py --input mop.pdf --output ./output --api-key sk-ant-...
```

---

## REST API Reference

All endpoints are prefixed with `/api/v1`.

### Upload and process a document

```bash
curl -X POST http://localhost:8000/api/v1/process \
  -F "file=@mop.pdf" \
  -F "model=claude-sonnet-4-6" \
  -F "title=BGP Migration MOP"
```

Response:
```json
{ "job_id": "abc12345-...", "status": "pending", "filename": "mop.pdf" }
```

### Poll status

```bash
curl http://localhost:8000/api/v1/status/abc12345-...
```

### Get result + download links

```bash
curl http://localhost:8000/api/v1/result/abc12345-...
```

### Download an artifact

```bash
# Zephyr CSV
curl -O http://localhost:8000/api/v1/download/abc12345-.../zephyr

# Robot Framework test
curl -O http://localhost:8000/api/v1/download/abc12345-.../robot

# CLI rules
curl -O http://localhost:8000/api/v1/download/abc12345-.../cli_rules

# Full canonical JSON
curl -O http://localhost:8000/api/v1/download/abc12345-.../canonical
```

### View per-job debug log

```bash
curl http://localhost:8000/api/v1/logs/abc12345-...
```

### List recent jobs

```bash
curl http://localhost:8000/api/v1/jobs
```

### With API key auth (if MOP_API_KEY is set)

```bash
curl -H "X-API-Key: your-platform-key" http://localhost:8000/api/v1/jobs
```

---

## Configuration

All options are set via environment variables (`.env` file or `docker compose`).

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | **Required.** Your Anthropic API key |
| `MOP_API_KEY` | *(empty)* | If set, all API calls require `X-API-Key` header |
| `MAX_WORKERS` | `3` | Max concurrent documents being processed |
| `LOG_LEVEL` | `INFO` | Console log level: `DEBUG` · `INFO` · `WARNING` · `ERROR` |
| `LOG_DIR` | `logs` | Directory for rotating log files |

---

## Logs and Debugging

**Server log file** (all requests + all job stages):
```
logs/mop_api.log       ← rotating, 10MB × 5 backups
```

**Per-job log** (without touching the file system):
```bash
curl http://localhost:8000/api/v1/logs/<job_id>
```

**Filter server log for one job:**
```bash
grep "job:abc12345" logs/mop_api.log
```

**Enable verbose DEBUG logging:**
```env
LOG_LEVEL=DEBUG
```

---

## Running Tests

```bash
# All 91 tests — no API key required (LLM is mocked)
pytest tests/unit_tests/test_pipeline.py -v

# Specific test group
pytest tests/unit_tests/test_pipeline.py -v -k "TOON"
pytest tests/unit_tests/test_pipeline.py -v -k "Vendor"
pytest tests/unit_tests/test_pipeline.py -v -k "Chunker"
```

---

## Project Structure

```
mop_ai_platform_repo/
│
├── api/                     # FastAPI layer
│   ├── main.py              #   App, CORS, request logging middleware
│   ├── routes.py            #   All API endpoints + background worker
│   ├── job_store.py         #   File-based job persistence (output/jobs/)
│   ├── auth.py              #   Optional X-API-Key authentication
│   └── logging_config.py   #   Console + rotating file + per-job log capture
│
├── static/
│   └── index.html           # Drag-and-drop web UI
│
├── pipeline.py              # CLI entrypoint (also used by API)
│
├── ingestion/               # Document parsers
│   ├── document_loader.py   #   Routes PDF/DOCX/TXT to correct parser
│   ├── pdf_parser.py        #   pdfplumber + PyPDF2 fallback
│   ├── docx_parser.py       #   python-docx with style detection
│   ├── txt_parser.py        #   Markdown + plain text
│   └── normalizer/          #   Structure detection (list/table/prose/mixed)
│
├── grammar_engine/
│   └── cli_grammar.py       # Multi-vendor CLI command extraction
│
├── toon/                    # Token compression (85-90% reduction)
│   ├── builder.py           #   ParsedDocument → TOONDocument (pure Python)
│   ├── renderer.py          #   TOONDocument → compact LLM-ready text
│   ├── compressor.py        #   Filler removal + prose scoring
│   └── models.py            #   TOONDocument / TOONSection / TOONNode
│
├── ai_layer/                # LLM orchestration
│   ├── super_prompt_runner.py  # Claude API calls, retry, chunking, TOON
│   ├── llm_result.py           # Typed result wrapper (never raises)
│   ├── context_chunker.py      # Section-based chunking for 50+ page docs
│   └── prompts/
│       ├── super_prompt.py     # Raw-text prompt templates
│       └── toon_prompt.py      # TOON-compressed prompt templates
│
├── post_processing/
│   ├── guardrails.py        # Post-LLM quality + coverage checks
│   └── schema_validator.py  # Pydantic validation
│
├── generators/
│   ├── zephyr_generator.py  # Zephyr Scale CSV
│   ├── robot_generator.py   # Robot Framework .robot
│   └── cli_rule_generator.py # CLI validation rules JSON
│
├── models/
│   └── canonical.py         # Shared Pydantic models (pipeline contract)
│
├── configs/
│   └── protocol_patterns.yaml  # Vendor + protocol patterns for grammar engine
│
├── tests/
│   └── unit_tests/
│       └── test_pipeline.py # 91 unit tests (all passing, no API key needed)
│
├── output/                  # Generated at runtime (gitignored)
│   ├── jobs/                #   Job state JSON files
│   └── uploads/             #   Temp uploaded files (auto-deleted after processing)
│
├── logs/                    # Log files (gitignored except .gitkeep)
│   └── mop_api.log
│
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── requirements.txt
```

---

## Supported Document Formats

| Format | Parser | Notes |
|--------|--------|-------|
| PDF | pdfplumber (primary), PyPDF2 (fallback) | Layout-aware, table extraction |
| DOCX | python-docx | Heading styles, numbered lists, tables, tracked changes |
| TXT / MD | Built-in | Markdown headings, numbered lists, bullet points, code fences |

## Supported Vendors

Cisco IOS/IOS-XE/IOS-XR/NX-OS · Juniper Junos · Nokia SR OS · Arista EOS ·
F5 BIG-IP TMSH · Palo Alto PAN-OS · Check Point Gaia/Clish · Huawei VRP · Ericsson IPOS · Generic/Unknown
