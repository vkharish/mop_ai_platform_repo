# MOP AI Platform

Convert any procedural network document (MOP, SOP, runbook) into structured test artifacts and executable automation — automatically, using Claude AI.

**Phase 1 — Document Intelligence**
- Extracts CLI commands, steps, expected outputs, rollback procedures from PDF/DOCX/TXT
- Scores MOP quality (HIGH / MEDIUM / LOW) with actionable recommendations
- Generates Zephyr Scale test cases, Robot Framework automation, CLI validation rules

**Phase 2 — Execution Engine**
- Executes the MOP against real network devices over SSH (Netmiko)
- DAG-based parallel execution with wave scheduling
- Automatic rollback on failure, per-step approval gates, kill switch
- Pre/post diff comparison to show exactly what changed on the device
- Notifications (Slack, Email, PagerDuty) and ITSM integration (ServiceNow, Jira)

**Phase 3 — Standalone Protocol Test Agent**
- Vendor-agnostic lab certification across BGP · IS-IS · MPLS · Interfaces · System
- Hierarchical inventory: per-vendor/model templates + topology instances
- Auto-discovers topology from a live seed device (LLDP/CDP) or plain English description
- Two modes: Python agent (automated, cheap) + Claude Code skills (interactive, reasoning)
- Pre/post baseline comparison with regression detection and change verdict

**Supports:** PDF · DOCX · TXT · Markdown
**Vendors:** Cisco IOS/IOS-XE/IOS-XR/NX-OS · Juniper Junos · Nokia SR OS · Arista EOS · F5 BIG-IP · Palo Alto · Check Point · Huawei VRP · Ericsson IPOS

---

## Quick Start

### Prerequisites

| Requirement | Version |
|------------|---------|
| Python | 3.10+ |
| Anthropic API key | [console.anthropic.com](https://console.anthropic.com) — optional, `--mock-llm` works without it |

```bash
git clone https://github.com/vkharish/mop_ai_platform_repo.git
cd mop_ai_platform_repo
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## Option 1 — Command Line (quickest)

```bash
# With real Claude API
export ANTHROPIC_API_KEY=sk-ant-...
python pipeline.py --input mop.pdf --output ./out

# Without API key (grammar engine only, no LLM call)
python pipeline.py --input mop.pdf --output ./out --mock-llm

# Dry-run: see exactly what would execute before touching any device
python pipeline.py --input mop.pdf --output ./out --mock-llm --dry-run

# All options
python pipeline.py \
  --input mop.pdf \
  --output ./out \
  --title "BGP Migration MOP" \
  --model claude-sonnet-4-6 \
  --mock-llm        # skip LLM (no API key needed)
  --dry-run         # print execution plan, save _dryrun.txt
  --skip-toon       # disable token compression
  --skip-guardrails # skip post-LLM quality checks
```

**Output files written to `./out/`:**
```
BGP_Migration_MOP.robot              ← Robot Framework test suite (SSH automation)
BGP_Migration_MOP_zephyr.csv         ← Zephyr Scale bulk import
BGP_Migration_MOP_cli_rules.json     ← CLI validation rules
BGP_Migration_MOP_canonical.json     ← Full structured data (all pipeline stages)
BGP_Migration_MOP_dryrun.txt         ← Execution plan (only with --dry-run)
```

Every run also prints a **MOP Quality Report**:
```
MOP QUALITY REPORT  ✅ HIGH
Quality: HIGH [████████░░] 9/12 (75%)

  Commands Detected      [▓▓▓] 3/3   24 commands
  Rollback Steps         [▓▓]  2/2   4 rollback steps
  Pre Checks             [▓]   1/1   present
  Verification Section   [▓]   1/1   present
  Expected Output Cover  [░░]  0/2   2/20 steps (10%)
  Command Confidence     [▓]   1/1   90% avg confidence
  Section Diversity      [▓]   1/1   4 sections
  Failure Strategy       [░]   0/1   abort

  Recommendations:
    → Define expected outputs for verification steps
    → Consider ROLLBACK_ALL for production MOPs
```

---

## Option 2 — Web UI + REST API

```bash
cp .env.example .env
# Edit .env: set ANTHROPIC_API_KEY

.venv/bin/uvicorn api.main:app --reload --port 8000
```

Open `http://localhost:8000` — drag-and-drop upload, live logs, artifact download.
Swagger UI: `http://localhost:8000/docs`

---

## Option 3 — Docker

```bash
cp .env.example .env
# Edit .env: set ANTHROPIC_API_KEY

docker compose up --build
# Open http://localhost:8000
```

---

## REST API Reference

### Phase 1 — Document Processing (`/api/v1`)

```bash
# Upload and process a document
curl -X POST http://localhost:8000/api/v1/process \
  -F "file=@mop.pdf" -F "model=claude-sonnet-4-6"

# Poll status
curl http://localhost:8000/api/v1/status/<job_id>

# Download artifacts
curl -O http://localhost:8000/api/v1/download/<job_id>/robot
curl -O http://localhost:8000/api/v1/download/<job_id>/zephyr
curl -O http://localhost:8000/api/v1/download/<job_id>/cli_rules
curl -O http://localhost:8000/api/v1/download/<job_id>/canonical
```

### Phase 2 — Execution Engine (`/api/v2`)

```bash
# Start execution
curl -X POST http://localhost:8000/api/v2/executions \
  -H "Content-Type: application/json" \
  -d '{"canonical_json_path": "out/mop_canonical.json", "dry_run": true}'

# Execution lifecycle
curl -X POST http://localhost:8000/api/v2/executions/<id>/pause
curl -X POST http://localhost:8000/api/v2/executions/<id>/resume
curl -X POST http://localhost:8000/api/v2/executions/<id>/abort
curl -X POST http://localhost:8000/api/v2/executions/<id>/rollback

# Approve a pending step
curl -X POST http://localhost:8000/api/v2/approvals/<id> \
  -d '{"decision": "approved", "approver": "neteng@company.com"}'

# Reports
curl http://localhost:8000/api/v2/executions/<id>/report
curl http://localhost:8000/api/v2/executions/<id>/report/html
curl http://localhost:8000/api/v2/executions/<id>/timeline

# Emergency kill switch (stops all active executions)
curl -X POST http://localhost:8000/api/v2/kill
curl -X DELETE http://localhost:8000/api/v2/kill   # clear

# Prometheus metrics
curl http://localhost:8000/api/v2/metrics
```

---

## Generated Files Explained

| File | Purpose |
|------|---------|
| `_canonical.json` | Machine-readable MOP — the contract between all pipeline stages. Contains every step, command, expected output, section, tags, blast_radius, rollback flag. This is what the Execution Engine reads. |
| `.robot` | Ready-to-run Robot Framework file. SSHes to the device, runs each step, checks for device errors after every command, auto-triggers rollback on failure. Run with: `robot --variable HOST:x.x.x.x --variable USERNAME:admin --variable PASSWORD:secret mop.robot` |
| `_cli_rules.json` | Per-command validation rules: vendor, protocol, mode, error patterns. Used by the ValidationAgent. |
| `_zephyr.csv` | Jira/Zephyr Scale import. Each row = one test case with folder path, steps, labels. Import directly into Zephyr. |
| `_dryrun.txt` | Human-readable execution plan: every step in order, exact commands, devices, expected outputs, rollback plan in reverse. No device is touched. |

---

## Robot Framework Failure Handling

The generated `.robot` file has full failure handling built in:

- **After every implementation command** → `Verify No Device Error` checks for `%`, `Error`, `Invalid`, `failed`, `timeout`
- **Per-step teardown** → logs failure context if a step fails
- **failure_strategy=ABORT** (default) → Robot stops on first failure
- **failure_strategy=CONTINUE** → adds `robot:continue-on-failure` tag, proceeds through failures
- **failure_strategy=ROLLBACK_ALL** → Suite Teardown auto-triggers `Execute Rollback Procedure` (runs rollback steps in reverse order)
- **Pre/Post diff** → pre-check outputs saved as baseline variables; verification steps automatically compare against baseline and log what changed

---

## Configuration

Set via `.env` file or environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | Anthropic API key (required for real LLM; optional with `--mock-llm`) |
| `MOP_API_KEY` | *(empty)* | If set, all API calls require `X-API-Key` header |
| `MAX_WORKERS` | `3` | Max concurrent documents |
| `LOG_LEVEL` | `INFO` | `DEBUG` · `INFO` · `WARNING` · `ERROR` |
| `SLACK_WEBHOOK_URL` | — | Slack notifications (optional) |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASS` | — | Email notifications (optional) |
| `PD_INTEGRATION_KEY` | — | PagerDuty alerts (optional) |
| `ITSM_BASE_URL` / `ITSM_USERNAME` / `ITSM_PASSWORD` | — | ServiceNow integration (optional) |

All notification/ITSM integrations are **dry-run safe** — if credentials are absent, they log with `[NOTIFICATION_DRY_RUN]` instead of failing.

---

## Phase 3 — Standalone Protocol Test Agent

A fully self-contained lab certification tool. Run protocol health tests against any vendor, any topology — without going through the full MOP pipeline.

### Python Agent (automated, no interaction needed)

```bash
# BGP gating tests — mock SSH (no real devices)
python standalone_tester/run_tests.py \
  --topology hybrid/sample_mpls_lab.yaml \
  --protocol bgp \
  --test gating \
  --mock-ssh --mock-llm

# Full certification — all protocols, real devices
export PE1_CREDS="admin:secret"
export PE2_CREDS="admin:secret"
python standalone_tester/run_tests.py \
  --topology hybrid/sample_mpls_lab.yaml \
  --protocol all \
  --test certification

# Filter to one device or vendor
python standalone_tester/run_tests.py \
  --topology hybrid/sample_mpls_lab.yaml \
  --protocol isis --test smoke \
  --device PE1 --mock-ssh --mock-llm

# Save report for baseline comparison later
python standalone_tester/run_tests.py \
  --topology hybrid/sample_mpls_lab.yaml \
  --protocol bgp --test gating \
  --output standalone_tester/reports/pre_change_$(date +%Y%m%d_%H%M%S).json \
  --mock-ssh --mock-llm
```

### Topology Discovery

```bash
# Generate topology from a live seed device (LLDP/CDP walk)
python standalone_tester/discover.py --seed 192.168.1.1

# Generate from plain English — no devices needed
python standalone_tester/discover.py \
  --describe "2 Cisco ASR9006 PEs running IOS-XR 7.5.1, 1 Nokia 7750 P router, connected via IS-IS and MPLS" \
  --name my_lab

# Generate sample topology for testing (no devices, no API key)
python standalone_tester/discover.py --mock
```

Generated topology files land in `standalone_tester/inventory/generated/`.

### Claude Code Skills (interactive, with full reasoning)

These skills run **inside Claude Code** and reason about WHY tests fail, not just THAT they fail. They are 10-40x more expensive in tokens than the Python agent but provide root cause analysis and recommendations.

```
/protocol-test --device PE1 --protocol bgp --test gating --mock-ssh
/protocol-test --topology hybrid/sample_mpls_lab.yaml --protocol all --test certification --mock-ssh

/discover-topology --mock
/discover-topology --seed 192.168.1.1
/discover-topology --describe "2 Cisco PEs running IOS-XR, 1 Nokia P"

/run-certification --topology hybrid/sample_mpls_lab.yaml --mock-ssh --mock-llm

/compare-baseline \
  --topology hybrid/sample_mpls_lab.yaml \
  --baseline standalone_tester/reports/pre_change_20260120_143000.json \
  --mock-ssh --mock-llm
```

Skills are available immediately in this repository. For global access on your machine:

```bash
# Copy skills to your Claude Code global config
cp -r .claude/skills/* ~/.claude/skills/
# Then restart Claude Code — skills appear in the /skills panel
```

### Inventory Structure

```
standalone_tester/
├── inventory/
│   ├── vendors/                    ← vendor/OS/model templates
│   │   ├── cisco/
│   │   │   ├── ios-xr/
│   │   │   │   ├── _defaults.yaml  ← IOS-XR defaults (commands, quirks)
│   │   │   │   ├── asr9000.yaml    ← ASR9000-specific capabilities
│   │   │   │   └── ncs5500.yaml
│   │   │   └── ios-xe/
│   │   │       └── _defaults.yaml
│   │   ├── juniper/junos/          ← mx-series.yaml + _defaults.yaml
│   │   ├── nokia/sros/             ← 7750-sr.yaml + _defaults.yaml
│   │   ├── arista/eos/
│   │   ├── huawei/vrp/
│   │   └── ericsson/ipos/
│   ├── topologies/
│   │   └── hybrid/
│   │       └── sample_mpls_lab.yaml ← devices reference vendor templates via ref:
│   └── generated/                  ← output of discover.py
│
├── test_catalog/
│   └── catalog.yaml                ← vendor-agnostic test intents per protocol/type
│
├── agent/
│   ├── inventory_manager.py        ← resolves ref: → merged vendor+model config
│   ├── catalog_manager.py          ← loads test intents for protocol+test_type
│   ├── command_translator.py       ← intent → vendor CLI (Haiku LLM + file cache)
│   ├── protocol_test_agent.py      ← orchestrates device × test execution
│   └── result_model.py             ← TestResult, DeviceTestReport, TestSuiteReport
│
├── discovery/
│   ├── version_detector.py         ← regex-based OS/vendor detection from show version
│   └── topology_discovery.py       ← LLDP/CDP live walk + LLM-from-description
│
├── cache/
│   └── command_cache.json          ← translated commands cached by vendor+intent_id
│
├── reports/                        ← timestamped JSON test reports
├── run_tests.py                    ← CLI entrypoint for Python agent
└── discover.py                     ← CLI entrypoint for topology discovery
```

Topology files reference vendor templates with a `ref:` field:
```yaml
devices:
  PE1:
    ref: cisco/ios-xr/asr9000   # → loads _defaults.yaml + asr9000.yaml, deep-merged
    version: "7.5.1"
    role: pe-router
    connection:
      host: "192.168.100.1"
    credentials_env: PE1_CREDS  # export PE1_CREDS="admin:secret"
```

### Test Catalog

Tests are vendor-agnostic **intents** — the Python agent translates them to vendor CLI at runtime:

| Protocol | Smoke | Gating | Certification |
|----------|-------|--------|---------------|
| BGP | neighbor state | session stability, prefixes, MED | full policy, communities, route reflection |
| IS-IS | adjacency | LSP database, SPF | convergence, multi-level, redistribution |
| MPLS | LDP/RSVP state | LSP end-to-end | TE, FRR, QoS, traffic stats |
| Interface | up/down | errors, MTU | full QoS, sub-interfaces, CRC |
| System | reachability | CPU/memory | NTP, logging, redundancy, filesystem |

---

## Running Tests

```bash
# All 223 tests — no API key required
.venv/bin/python -m pytest tests/unit_tests/ -v

# By group
.venv/bin/python -m pytest tests/unit_tests/test_pipeline.py -v              # Phase 1 (121 tests)
.venv/bin/python -m pytest tests/unit_tests/test_agents.py -v                # Execution agents (33 tests)
.venv/bin/python -m pytest tests/unit_tests/test_phase2_integration.py -v    # Phase 2 + enhancements (37 tests)
.venv/bin/python -m pytest tests/unit_tests/test_standalone_tester.py -v     # Phase 3 standalone agent (32 tests)

# By feature
.venv/bin/python -m pytest -v -k "Quality"       # quality scorer
.venv/bin/python -m pytest -v -k "Diff"          # pre/post diff engine
.venv/bin/python -m pytest -v -k "DryRun"        # dry-run plan
.venv/bin/python -m pytest -v -k "TOON"          # token compression
.venv/bin/python -m pytest -v -k "Inventory"     # inventory manager
.venv/bin/python -m pytest -v -k "Catalog"       # test catalog
.venv/bin/python -m pytest -v -k "Translator"    # command translator
.venv/bin/python -m pytest -v -k "Discovery"     # topology discovery
```

---

## Project Structure

```
mop_ai_platform_repo/
│
├── pipeline.py                  # CLI entrypoint — runs full Phase 1 pipeline
│
├── ingestion/                   # Document parsers
│   ├── document_loader.py       #   Routes PDF/DOCX/TXT to correct parser
│   ├── pdf_parser.py            #   pdfplumber + PyPDF2 fallback
│   ├── docx_parser.py           #   python-docx, heading/table/list detection
│   ├── txt_parser.py            #   Markdown + plain text
│   ├── ocr_fallback.py          #   Tesseract OCR for scanned PDFs
│   └── normalizer/              #   Structure detection (list/table/prose/mixed)
│
├── grammar_engine/
│   └── cli_grammar.py           # Multi-vendor CLI command extraction (two-pass)
│
├── toon/                        # TOON token compression (85–90% reduction)
│   ├── builder.py
│   ├── renderer.py
│   ├── compressor.py
│   └── models.py
│
├── ai_layer/                    # LLM orchestration
│   ├── super_prompt_runner.py   #   Claude API calls, retry, chunking
│   ├── mock_llm_runner.py       #   Offline mode — no API key needed
│   ├── llm_result.py            #   Typed result wrapper
│   ├── context_chunker.py       #   Section-based chunking for large docs
│   └── prompts/
│
├── post_processing/
│   ├── guardrails.py            # Post-LLM quality + coverage checks
│   └── schema_validator.py      # Pydantic validation
│
├── quality/
│   └── quality_scorer.py        # MOP quality scoring (HIGH/MEDIUM/LOW, 8 criteria)
│
├── generators/
│   ├── zephyr_generator.py      # Zephyr Scale CSV
│   ├── robot_generator.py       # Robot Framework .robot (with failure handling + pre/post diff)
│   └── cli_rule_generator.py    # CLI validation rules JSON
│
├── models/
│   └── canonical.py             # Shared Pydantic models — pipeline contract
│
├── execution_engine/            # Phase 2 — executes MOP on real devices
│   ├── planner_agent.py         #   DAG build, wave scheduling, approval check
│   ├── execution_agent.py       #   Wave-parallel SSH execution loop
│   ├── validation_agent.py      #   Output validation (regex + active rules)
│   ├── recovery_agent.py        #   Retry, rollback, decision log
│   ├── dag_engine.py            #   Kahn's algorithm, critical path
│   ├── state_manager.py         #   In-memory execution state store
│   ├── concurrency_controller.py#   Per-device locks + global semaphore
│   ├── kill_switch.py           #   Emergency stop (threading.Event + file sentinel)
│   └── models.py                #   ExecutionPlan, ExecutionState
│
├── device_layer/
│   ├── device_driver.py         # Netmiko SSH driver + MockDriver for tests
│   ├── connection_pool.py       # SSH connection reuse
│   └── credential_store.py     # Encrypted credential storage
│
├── smart_wait/
│   ├── polling_engine.py        # Wait-for-condition polling
│   └── idempotency_engine.py    # Skip-if-already-applied checks
│
├── safety/
│   ├── rbac.py                  # Role-based access control
│   └── maintenance_window.py    # Enforce change windows
│
├── notifications/
│   ├── notification_router.py   # Dispatches to all notifiers
│   ├── slack_notifier.py        # Slack webhook
│   ├── email_notifier.py        # SMTP email
│   └── pagerduty_notifier.py    # PagerDuty Events API v2
│
├── itsm/
│   ├── itsm_client.py           # Facade — routes to ServiceNow or Jira
│   ├── servicenow_adapter.py    # REST Table API
│   └── jira_adapter.py          # Jira REST API v3 + ADF comments
│
├── reporting/
│   ├── execution_report.py      # JSON + HTML execution report (Jinja2)
│   └── diff_engine.py           # Pre/post CLI output diff + MOP version diff
│
├── api/
│   ├── main.py                  # FastAPI app, CORS, /health
│   ├── routes.py                # Phase 1 endpoints (/api/v1)
│   ├── execution_routes.py      # Phase 2 endpoints (/api/v2) — 15 endpoints
│   ├── job_store.py             # File-based job persistence
│   ├── auth.py                  # Optional X-API-Key auth
│   └── logging_config.py        # Per-job log capture
│
├── configs/
│   ├── protocol_patterns.yaml   # Vendor + protocol patterns for grammar engine
│   ├── execution_defaults.yaml  # Timeouts, concurrency, blocked commands
│   ├── device_inventory.yaml    # Device hostnames + credentials references
│   ├── rbac.yaml                # User roles and permissions
│   └── notifications.yaml       # Notification routing rules
│
├── standalone_tester/               # Phase 3 — standalone protocol test agent
│   ├── run_tests.py                 #   CLI: python run_tests.py --topology ... --protocol bgp
│   ├── discover.py                  #   CLI: python discover.py --seed <ip> | --describe "..." | --mock
│   ├── inventory/
│   │   ├── vendors/                 #   Per-vendor/OS/model YAML templates
│   │   │   ├── cisco/ios-xr/        #   _defaults.yaml + asr9000.yaml + ncs5500.yaml
│   │   │   ├── cisco/ios-xe/        #   _defaults.yaml
│   │   │   ├── juniper/junos/       #   _defaults.yaml + mx-series.yaml
│   │   │   ├── nokia/sros/          #   _defaults.yaml + 7750-sr.yaml
│   │   │   ├── arista/eos/          #   _defaults.yaml
│   │   │   ├── huawei/vrp/          #   _defaults.yaml
│   │   │   └── ericsson/ipos/       #   _defaults.yaml
│   │   ├── topologies/
│   │   │   └── hybrid/
│   │   │       └── sample_mpls_lab.yaml  # 4-device mixed-vendor topology
│   │   └── generated/               #   Output of discover.py
│   ├── test_catalog/
│   │   └── catalog.yaml             #   Vendor-agnostic test intents (5 protocols × 3 types)
│   ├── agent/
│   │   ├── inventory_manager.py     #   Resolves ref: → merged vendor+model config
│   │   ├── catalog_manager.py       #   Loads test intents for protocol+test_type
│   │   ├── command_translator.py    #   Intent → vendor CLI (Haiku LLM + file cache)
│   │   ├── protocol_test_agent.py   #   Orchestrates device × test execution
│   │   └── result_model.py          #   TestResult, DeviceTestReport, TestSuiteReport
│   ├── discovery/
│   │   ├── version_detector.py      #   Regex OS/vendor detection from show version
│   │   └── topology_discovery.py    #   LLDP/CDP live walk + LLM-from-description
│   ├── cache/
│   │   └── command_cache.json       #   Cached vendor CLI translations
│   └── reports/                     #   Timestamped JSON test reports
│
├── .claude/
│   └── skills/                      # Claude Code interactive skills
│       ├── protocol-test/SKILL.md   #   /protocol-test — investigate WHY tests fail
│       ├── discover-topology/SKILL.md #  /discover-topology — live or LLM topology gen
│       ├── run-certification/SKILL.md # /run-certification — full cert suite + verdict
│       └── compare-baseline/SKILL.md  # /compare-baseline — regression detection
│
├── tests/
│   └── unit_tests/
│       ├── test_pipeline.py              # 121 Phase 1 tests
│       ├── test_agents.py                # 33 execution agent tests
│       ├── test_phase2_integration.py    # 37 Phase 2 + enhancement tests
│       └── test_standalone_tester.py     # 32 Phase 3 standalone agent tests
│
├── MOP_AI_PLATFORM_DESIGN_v4.md # Complete architecture design document
├── .env.example
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

---

## Supported Document Formats

| Format | Parser | Notes |
|--------|--------|-------|
| PDF | pdfplumber (primary), PyPDF2 (fallback) | Layout-aware, table extraction |
| DOCX | python-docx | Heading styles, numbered lists, tables, tracked changes |
| TXT / MD | Built-in | Markdown headings, code fences, numbered/bulleted lists |

## Supported Vendors

Cisco IOS · IOS-XE · IOS-XR · NX-OS · Juniper Junos · Nokia SR OS · Arista EOS ·
F5 BIG-IP TMSH · Palo Alto PAN-OS · Check Point Gaia/Clish · Huawei VRP · Ericsson IPOS · Generic
