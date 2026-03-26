# MOP AI Platform — Complete Design Document v4

**Date:** 2026-03-25
**Version:** 4.0
**Status:** Living document — reflects all implemented code as of this date

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Repository Layout](#2-repository-layout)
3. [Central Data Contract — CanonicalTestModel](#3-central-data-contract--canonicaltestmodel)
4. [Phase 1 — Document Ingestion Pipeline](#4-phase-1--document-ingestion-pipeline)
5. [Grammar Engine — CLI Command Detection](#5-grammar-engine--cli-command-detection)
6. [TOON — Tree of Outlined Nodes](#6-toon--tree-of-outlined-nodes)
7. [AI Layer — LLM Extraction](#7-ai-layer--llm-extraction)
8. [Post-Processing — Guardrails and Schema Validation](#8-post-processing--guardrails-and-schema-validation)
9. [Generators — Output Artifacts](#9-generators--output-artifacts)
10. [Phase 2 — Execution Engine Architecture](#10-phase-2--execution-engine-architecture)
11. [DAG Engine — Dependency and Wave Planning](#11-dag-engine--dependency-and-wave-planning)
12. [State Manager — Execution Persistence](#12-state-manager--execution-persistence)
13. [Sequential Agents — Planner, Executor, Validator, Recovery](#13-sequential-agents--planner-executor-validator-recovery)
14. [Device Layer — Drivers, Credentials, Connection Pool](#14-device-layer--drivers-credentials-connection-pool)
15. [Safety Systems — Kill Switch, RBAC, Maintenance Window](#15-safety-systems--kill-switch-rbac-maintenance-window)
16. [Smart Wait — Polling Engine and Idempotency](#16-smart-wait--polling-engine-and-idempotency)
17. [Notifications](#17-notifications)
18. [ITSM Integration](#18-itsm-integration)
19. [Reporting](#19-reporting)
20. [REST API — Phase 1 and Phase 2 Endpoints](#20-rest-api--phase-1-and-phase-2-endpoints)
21. [Configuration Files](#21-configuration-files)
22. [Test Suite](#22-test-suite)
23. [Deployment and Operations](#23-deployment-and-operations)

---

## 1. System Overview

The MOP AI Platform converts human-authored Method of Procedure (MOP) and Standard Operating Procedure (SOP) documents into structured, executable network automation artifacts. It operates in two phases.

**Phase 1 — Document to Structured Data:**
A network operations document (PDF, DOCX, or TXT) is uploaded. The platform ingests it, runs a grammar-engine baseline pass to detect CLI commands, optionally compresses it into TOON format to reduce LLM token cost, sends it to Claude (Anthropic API) for structured extraction, validates the output, and emits three artifacts:
- A Zephyr Scale CSV for test management import
- A Robot Framework `.robot` file for automated execution
- A CLI validation rules JSON file

**Phase 2 — Structured Data to Live Execution:**
The `CanonicalTestModel` JSON produced in Phase 1 is submitted to an execution engine that: plans a wave-based execution schedule (DAG), manages SSH connections to real network devices, executes each step with configurable retry/failure/rollback logic, validates output, and streams status updates via a REST API.

**Key design choices:**
- The `CanonicalTestModel` is the single immutable contract between every pipeline stage. No stage reaches back to the raw document.
- TOON achieves 85–90% token reduction for structured documents, making large MOP files practical to process within Claude's context window.
- All Phase 2 safety mechanisms (kill switch, RBAC, maintenance windows, concurrency limits) are independent layers that do not require modifying the execution agents.
- Every external call (LLM, device SSH, ITSM, notifications) has a try/except wrapper; no external failure can crash the core pipeline.

**Technology stack:**

| Component | Technology |
|-----------|------------|
| Language | Python 3.11+ |
| Data model | Pydantic v2 |
| API server | FastAPI + Uvicorn |
| LLM | Anthropic Claude (claude-sonnet-4-6 / claude-opus-4-6) |
| PDF parsing | pdfplumber (primary), PyPDF2 (fallback) |
| DOCX parsing | python-docx + lxml |
| SSH execution | Netmiko + Paramiko |
| Credential encryption | AES-256-GCM (cryptography library) |
| Test runner | pytest |
| Container | Docker + docker-compose |

---

## 2. Repository Layout

```
mop_ai_platform_repo/
├── pipeline.py                    # Phase 1 orchestrator (CLI entry point)
├── requirements.txt
├── docker-compose.yml
│
├── models/
│   └── canonical.py               # CanonicalTestModel and all enums/sub-models
│
├── ingestion/
│   ├── document_loader.py         # Magic-byte routing to format parsers
│   ├── pdf_parser.py              # pdfplumber + PyPDF2 + OCR trigger
│   ├── docx_parser.py             # python-docx + tracked-change stripping
│   ├── txt_parser.py              # Markdown/code-fence/plain text
│   ├── ocr_fallback.py            # pdf2image + pytesseract (soft dependency)
│   └── normalizer/
│       └── __init__.py            # detect_structure() → table/list/prose/mixed
│
├── grammar_engine/
│   ├── cli_grammar.py             # Two-pass command detection
│   └── protocol_patterns.yaml    # Vendor patterns and protocol keywords
│
├── toon/
│   ├── models.py                  # TOONNode, TOONSection, TOONDocument
│   ├── builder.py                 # Structure routing, node construction
│   ├── compressor.py              # TextCompressor + ProseAnalyzer
│   └── renderer.py                # TOON text format serialization
│
├── ai_layer/
│   ├── super_prompt_runner.py     # Main LLM runner (TOON path + raw path)
│   ├── context_chunker.py         # Greedy bin-packing chunker
│   ├── llm_result.py              # LLMResult typed wrapper + LLMErrorType
│   ├── mock_llm_runner.py         # Offline testing without API key
│   └── prompts/
│       └── super_prompt.py        # System prompt, templates, retry messages
│
├── post_processing/
│   ├── guardrails.py              # 6 semantic checks on CanonicalTestModel
│   └── schema_validator.py        # Pydantic round-trip + business rules
│
├── generators/
│   ├── zephyr_generator.py        # Zephyr Scale CSV bulk import
│   ├── robot_generator.py         # Robot Framework .robot files
│   └── cli_rule_generator.py      # CLI validation rules JSON
│
├── execution_engine/
│   ├── models.py                  # StepResult, ExecutionState, ExecutionPlan, etc.
│   ├── dag_engine.py              # Kahn's algorithm, wave generation, critical path
│   ├── state_manager.py           # File-backed execution state + TTL sweep
│   ├── kill_switch.py             # Two-layer kill (threading.Event + file sentinel)
│   ├── planner_agent.py           # PlannerAgent: plan computation
│   ├── execution_agent.py         # ExecutionAgent: wave loop + per-step execution
│   ├── validation_agent.py        # ValidationAgent: output matching
│   ├── recovery_agent.py          # RecoveryAgent: retry/rollback decisions
│   └── concurrency_controller.py  # BoundedSemaphore + per-device Lock
│
├── device_layer/
│   ├── device_driver.py           # Abstract DeviceDriver, MockDriver, NetmikoDriver
│   ├── connection_pool.py         # Connection reuse with idle-sweep daemon
│   └── credential_store.py        # Vault → env → encrypted file resolution
│
├── safety/
│   ├── rbac.py                    # 4-level RBAC via X-Api-Key header
│   └── maintenance_window.py      # Time-window enforcement
│
├── smart_wait/
│   ├── polling_engine.py          # Exponential backoff polling
│   └── idempotency_engine.py      # PROCEED/SKIP/PARTIAL_STATE verdicts
│
├── notifications/
│   ├── notification_router.py     # Event fan-out to all notifiers
│   ├── slack_notifier.py          # Slack webhook
│   ├── email_notifier.py          # SMTP HTML email
│   └── pagerduty_notifier.py      # PagerDuty Events v2 API
│
├── itsm/
│   ├── itsm_client.py             # Facade routing to adapter
│   ├── servicenow_adapter.py      # ServiceNow PATCH change_request
│   └── jira_adapter.py            # Jira ADF comment + dynamic transition
│
├── reporting/
│   └── execution_report.py        # JSON + HTML report builder
│
├── api/
│   ├── main.py                    # FastAPI app, lifespan, middleware, CORS
│   ├── routes.py                  # Phase 1 endpoints (/api/v1)
│   ├── execution_routes.py        # Phase 2 endpoints (/api/v2)
│   ├── auth.py                    # API key auth
│   ├── job_store.py               # File-backed job state for Phase 1
│   └── logging_config.py          # Rotating file + console handlers
│
├── configs/
│   ├── execution_defaults.yaml    # Retry, timeout, concurrency, blast radius
│   ├── device_inventory.yaml      # Device catalog (hostname, IP, platform)
│   ├── rbac.yaml                  # API key → role mapping
│   └── notifications.yaml         # Notifier enable/event configuration
│
├── static/
│   └── index.html                 # Single-page web UI
│
└── tests/
    └── unit_tests/
        ├── test_pipeline.py       # 121 tests
        ├── test_agents.py         # 33 tests
        └── test_phase2_integration.py  # 21 tests
```

---

## 3. Central Data Contract — CanonicalTestModel

**File:** `models/canonical.py`

The `CanonicalTestModel` is a Pydantic v2 model that serves as the single contract flowing through every stage of the platform. Every pipeline stage consumes and produces (or validates) this model. No stage reads the original document.

### 3.1 Enumerations

```python
class StepType(str, Enum):
    ACTION       = "action"
    VERIFICATION = "verification"
    ROLLBACK     = "rollback"
    INFO         = "info"

class ActionType(str, Enum):
    CONFIGURE = "configure"
    VERIFY    = "verify"
    ROLLBACK  = "rollback"
    EXECUTE   = "execute"
    OBSERVE   = "observe"

class FailureStrategy(str, Enum):
    ABORT          = "abort"
    CONTINUE       = "continue"
    ROLLBACK_ALL   = "rollback_all"
    ROLLBACK_GROUP = "rollback_group"

class ExecutionStatus(str, Enum):
    PENDING     = "pending"
    RUNNING     = "running"
    PASSED      = "passed"
    FAILED      = "failed"
    SKIPPED     = "skipped"
    ABORTED     = "aborted"
    ROLLED_BACK = "rolled_back"
    PAUSED      = "paused"

class ApprovalStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING      = "pending"
    APPROVED     = "approved"
    REJECTED     = "rejected"

class BlastRadius(str, Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"
```

### 3.2 Sub-models

**CLICommand** — one CLI command within a step:
```python
class CLICommand(BaseModel):
    raw:        str                  # Exact text as it appears in the document
    normalized: Optional[str]        # Lowercased, collapsed whitespace
    vendor:     Optional[str]        # cisco, juniper, nokia, arista, ...
    protocol:   Optional[str]        # bgp, ospf, isis, mpls, ...
    mode:       Optional[str]        # config, exec, ...
    confidence: float = 0.0          # Grammar engine detection confidence
```

**ExecutionPolicy** — per-step execution behavior:
```python
class ExecutionPolicy(BaseModel):
    retry_count:      int   = 3
    retry_delay_s:    float = 10.0
    timeout_s:        float = 30.0
    continue_on_fail: bool  = False
    rollback_on_fail: bool  = False
```

**IdempotencyRule** — skip logic:
```python
class IdempotencyRule(BaseModel):
    check_command: str
    skip_if_pattern: str    # Regex; if matches → SKIP
    skip_patterns:   List[str] = []
```

**ValidationRule** — active validation:
```python
class ValidationRule(BaseModel):
    command:        str
    expect_pattern: str       # Regex or substring
    negate:         bool = False
```

**StepTiming** — scheduling hints:
```python
class StepTiming(BaseModel):
    delay_before_s: float = 0.0
    delay_after_s:  float = 0.0
    max_wait_s:     float = 300.0
```

**ITSMRef** — change ticket linkage:
```python
class ITSMRef(BaseModel):
    system:    str   # "servicenow" | "jira"
    ticket_id: str
    sys_id:    Optional[str]  # ServiceNow internal ID
```

**DeviceRef** — device targeting:
```python
class DeviceRef(BaseModel):
    hostname: str
    ip:       Optional[str]
    platform: Optional[str]
    role:     Optional[str]
```

### 3.3 TestStep

The core unit of the model. Every action, verification, and rollback step is a `TestStep`.

```python
class TestStep(BaseModel):
    # Identity
    step_id:    str
    sequence:   int
    step_type:  StepType
    action_type: ActionType

    # Content
    description:     str
    raw_text:        str
    commands:        List[CLICommand] = []
    expected_output: Optional[str] = None
    section:         Optional[str] = None
    subsection:      Optional[str] = None

    # Flags
    is_rollback:      bool = False
    tags:             List[str] = []

    # Phase 2 execution fields
    devices:          List[DeviceRef] = []
    depends_on:       List[str] = []          # step_ids
    execution_policy: ExecutionPolicy = ExecutionPolicy()
    idempotency_rule: Optional[IdempotencyRule] = None
    validation_rules: List[ValidationRule] = []
    timing:           StepTiming = StepTiming()
    blast_radius:     BlastRadius = BlastRadius.LOW
    approval_required: bool = False
    savepoint:        bool = False
    transaction_group: Optional[str] = None
```

### 3.4 CanonicalTestModel

```python
class CanonicalTestModel(BaseModel):
    document_title:   str
    source_file:      str
    source_format:    str                    # pdf | docx | txt
    mop_structure:    str                    # table | numbered_list | prose | mixed
    steps:            List[TestStep]
    failure_strategy: Optional[FailureStrategy] = None
    change_ticket:    Optional[ITSMRef] = None
    approval_required: bool = False
    metadata:         Dict[str, Any] = {}
    created_at:       datetime = Field(default_factory=datetime.utcnow)
```

### 3.5 Parsed Document Types

The ingestion layer produces these intermediate types (never cross pipeline stage boundaries):

```python
class DocumentBlock(BaseModel):
    block_type: str      # heading | paragraph | table | list_item | code
    text:       str
    level:      int = 0  # Heading depth
    raw:        str = ""

class ParsedDocument(BaseModel):
    title:     str
    blocks:    List[DocumentBlock]
    metadata:  Dict[str, Any] = {}
    format:    str
```

---

## 4. Phase 1 — Document Ingestion Pipeline

**File:** `pipeline.py`

The pipeline is a 6-stage sequential orchestrator. It is both the CLI entry point and the function called by the Phase 1 API endpoint.

### 4.1 CLI Usage

```bash
python pipeline.py \
  --input bgp_mop.pdf \
  --output ./out \
  --title "BGP Route Policy Change" \
  --model claude-sonnet-4-6 \
  --api-key $ANTHROPIC_API_KEY

# Offline / no API key:
python pipeline.py --input mop.pdf --output ./out --mock-llm

# Skip TOON compression (send raw text to LLM):
python pipeline.py --input mop.pdf --output ./out --skip-toon

# Skip guardrail checks:
python pipeline.py --input mop.pdf --output ./out --skip-guardrails
```

### 4.2 Stage Sequence

```
Stage 1: Document Load
  document_loader.py → ParsedDocument
        ↓
Stage 2: Grammar Baseline
  cli_grammar.py → List[DetectedCommand]
        ↓
Stage 3: TOON Build (unless --skip-toon)
  toon/builder.py → TOONDocument
        ↓
Stage 4: LLM Extraction
  super_prompt_runner.py (or mock_llm_runner.py) → LLMResult → CanonicalTestModel
        ↓
Stage 5: Guardrails (unless --skip-guardrails)
  guardrails.py → GuardrailResult (warnings/errors logged; errors raise)
        ↓
Stage 6: Schema Validation
  schema_validator.py → validated CanonicalTestModel
        ↓
Output Generation (parallel, all three always run)
  zephyr_generator.py → {title}_zephyr.csv
  robot_generator.py  → {title}.robot
  cli_rule_generator.py → {title}_cli_rules.json
  schema_validator.to_json() → {title}_canonical.json
```

### 4.3 Output Directory Layout

Each run creates:
```
{output_dir}/
├── {safe_title}_canonical.json
├── {safe_title}_zephyr.csv
├── {safe_title}.robot
└── {safe_title}_cli_rules.json
```

When invoked via the API, output lands in `output/jobs/{job_id}/`.

### 4.4 Document Loader

**File:** `ingestion/document_loader.py`

Magic-byte detection determines file format before routing. This prevents content-type header spoofing (a user uploading a DOCX with a `.pdf` extension).

```python
_PDF_MAGIC = b"%PDF"
_ZIP_MAGIC = b"PK\x03\x04"   # All DOCX/XLSX/PPTX are ZIP

def load(path: str) -> ParsedDocument:
    with open(path, "rb") as f:
        header = f.read(8)
    if header.startswith(_PDF_MAGIC):
        return pdf_parser.parse(path)
    if header.startswith(_ZIP_MAGIC):
        # Confirm DOCX by checking for word/ in ZIP namelist
        import zipfile
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
        if any(n.startswith("word/") for n in names):
            return docx_parser.parse(path)
    return txt_parser.parse(path)  # UTF-8 sniff as last resort
```

### 4.5 PDF Parser

**File:** `ingestion/pdf_parser.py`

Primary library is `pdfplumber`. Tables are extracted first (before text), then text words are grouped into lines by y-coordinate with ±3px tolerance to reconstruct reading order.

Heading detection heuristics:
- Font size ≥ 14 → heading
- All-caps text shorter than 80 characters → heading

OCR fallback is triggered when fewer than 10% of pages have ≥ 20 characters of extracted text (indicating a scanned document).

PyPDF2 is the fallback if pdfplumber fails or returns empty content.

### 4.6 DOCX Parser

**File:** `ingestion/docx_parser.py`

Tracked-changes stripping is implemented before content extraction. The lxml iterator is used to find all `w:del` and `w:ins` elements. Removals and unwraps are collected into separate lists first, then applied, to avoid modifying the tree during iteration (which would corrupt the iterator state).

```python
# Collect first
to_remove = [el for el in tree.iter() if el.tag.endswith("}del")]
to_unwrap  = [el for el in tree.iter() if el.tag.endswith("}ins")]

# Then apply
for el in to_remove:
    el.getparent().remove(el)
for el in to_unwrap:
    parent = el.getparent()
    idx = list(parent).index(el)
    for child in list(el):
        parent.insert(idx, child)
        idx += 1
    parent.remove(el)
```

### 4.7 TXT Parser

**File:** `ingestion/txt_parser.py`

Handles five block patterns in order:
1. Code fences (``` opening/closing)
2. 4-space indented code blocks
3. `#` markdown headings (level = number of `#` chars)
4. Numbered list items (`1.`, `2.`, etc.)
5. Bullet list items (`-`, `*`, `•`)
6. Plain paragraphs

### 4.8 Structure Normalizer

**File:** `ingestion/normalizer/__init__.py`

`detect_structure()` counts block types across the parsed document and applies thresholds:

| Condition | Result |
|-----------|--------|
| table_ratio > 0.4 | `table` |
| list_ratio > 0.5, items are numbered | `numbered_list` |
| list_ratio > 0.5, items are bulleted | `bulleted_list` |
| prose_ratio > 0.7 | `prose` |
| ≥ 2 conditions partially met | `mixed` |

The structure string is stored in `CanonicalTestModel.mop_structure` and used by the TOON builder for routing decisions.

### 4.9 OCR Fallback

**File:** `ingestion/ocr_fallback.py`

Soft dependency — if `pdf2image` and `pytesseract` are not installed, `ocr_fallback.extract()` returns `None` instead of raising. The PDF parser then falls through to PyPDF2.

When OCR runs: DPI=300, each page rendered to PIL image, passed to `pytesseract.image_to_string()`.

To enable:
```
pip install pdf2image pytesseract Pillow
# Also requires Tesseract binary installed on the OS
```

---

## 5. Grammar Engine — CLI Command Detection

**Files:** `grammar_engine/cli_grammar.py`, `grammar_engine/protocol_patterns.yaml`

The grammar engine provides a pre-LLM baseline of detected CLI commands. Its output serves two purposes:
1. Feed the mock LLM runner (for offline/testing use)
2. Power the command_coverage guardrail check (post-LLM validation)

### 5.1 Two-Pass Detection

**Pass 1 — Vendor-specific patterns:**
Each vendor defines regex patterns in `protocol_patterns.yaml`. Vendor patterns are tried first. A match awards a confidence of `0.7 + boost` where boost is pattern-specific (e.g., `show bgp summary` is high-confidence).

**Pass 2 — Generic CLI indicators:**
Fallback for lines that look like CLI commands but didn't match any vendor. Uses a set of `generic_cli_indicators` patterns. Base confidence: `0.5`.

### 5.2 Supported Vendors and Protocols

| Vendor | Netmiko Device Type |
|--------|---------------------|
| cisco (IOS-XR) | cisco_xr |
| juniper (JunOS) | juniper_junos |
| nokia (SR OS) | nokia_sros |
| arista (EOS) | arista_eos |
| huawei (VRP) | huawei_vrp |
| f5 (BIG-IP) | linux |
| palo_alto (PAN-OS) | paloalto_panos |
| checkpoint (Gaia) | checkpoint_gaia |
| generic | cisco_ios |

Detected protocols: `bgp`, `ospf`, `isis`, `mpls`, `ldp`, `rsvp`, `vrrp`, `lacp`, `bfd`, `vxlan`, `evpn`, `stp`, `lldp`, `cdp`, `snmp`, `ntp`, `syslog`.

### 5.3 Protocol Detection Logic

Protocol is detected in two steps:
1. **Pass 1 (command prefix):** The command verb/prefix maps directly to a protocol.
2. **Pass 2 (keyword scan):** Words in the command are matched against protocol keywords.

Ambiguous keywords that appear across multiple protocols are intentionally skipped in Pass 2: `neighbor`, `area`, `level`, `mode`.

### 5.4 Prompt Stripping

Before pattern matching, line prefixes that are device prompts are stripped using compiled regex patterns:
```
hostname#, hostname>, Router(config)#, RP/0/RSP0/CPU0:hostname#, etc.
```

### 5.5 DetectedCommand Output

```python
@dataclass
class DetectedCommand:
    raw:        str      # Original text after prompt strip
    normalized: str      # Lowercase + collapsed whitespace
    vendor:     str      # Detected vendor or "generic"
    protocol:   str      # Detected protocol or ""
    mode:       str      # "config" | "exec" | ""
    confidence: float
```

---

## 6. TOON — Tree of Outlined Nodes

**Files:** `toon/models.py`, `toon/builder.py`, `toon/compressor.py`, `toon/renderer.py`

TOON is a custom compression format that converts structured MOP documents into a compact text representation before sending to the LLM. For a typical 50-page structured MOP, TOON achieves 85–90% token reduction.

### 6.1 TOON Models

```python
class TOONNodeType(str, Enum):
    SECTION    = "section"
    LIST_STEP  = "list_step"
    TABLE_STEP = "table_step"
    PROSE_STEP = "prose_step"
    CODE_STEP  = "code_step"

class TOONNode(BaseModel):
    node_id:         str                # Hierarchical: "s2.3", "s2.3.1"
    node_type:       TOONNodeType
    description:     str                # ≤120 chars, filler-stripped
    commands:        List[str] = []     # CLI commands verbatim
    expected_output: Optional[str]
    is_rollback:     bool = False
    confidence:      float = 0.0

class TOONSection(BaseModel):
    section_id: str
    title:      str
    mode:       str          # "toon" | "text"
    nodes:      List[TOONNode] = []
    raw_text:   str = ""     # Used when mode="text"

class TOONDocument(BaseModel):
    document_title:    str
    toon_usable:       bool
    fallback_reason:   str = ""
    compression_ratio: float = 0.0
    sections:          List[TOONSection] = []
```

### 6.2 Structure Routing

**File:** `toon/builder.py`

The builder routes each section to either TOON mode or raw text mode based on its structure:

| MOP Structure | TOON Mode |
|---------------|-----------|
| `numbered_list` | `toon` |
| `bulleted_list` | `toon` |
| `table` | `toon` |
| `prose` | `text` (raw passthrough) |
| `unknown` | `text` |
| `mixed` | Per-section routing |

For `mixed` documents, each section is independently classified. The `_TOON_SAFE_RATIO` constant (0.4) is the minimum fraction of sections that must be TOON-suitable for the document to be flagged `toon_usable=True`. If below this threshold, `_build_text_fallback()` is called, which returns the full document as raw text with `toon_usable=False`.

Table rows are buffered during processing and flushed as a batch when a non-table block is encountered.

Prose sections are scored by `ProseAnalyzer` to determine if they contain enough actionable content to be worth including in TOON format.

### 6.3 Text Compression

**File:** `toon/compressor.py`

`TextCompressor` applies 22 filler-phrase removal patterns before the node description is stored. Examples of removed patterns:
- "please note that", "it is important to", "make sure to", "in order to"
- "the following", "as shown below", "refer to", "per the"

After filler removal, text is truncated at a word boundary to ≤120 characters.

**ProseAnalyzer.score()** awards points for technical signal:

| Pattern | Max points |
|---------|-----------|
| Action verbs (configure, enable, set, apply, ...) | 3 |
| IPv4 addresses | 2 |
| IPv6 addresses | 2 |
| Interface names (Gi0/0, Et1/1, xe-0/0/0, ...) | 2 |
| AS numbers | 1 |
| VLAN IDs | 1 |
| IP prefixes | 1 |
| Hostnames | 1 |

Prose sections with score ≥ 3 are included in TOON output.

`ProseAnalyzer.extract_expected()` matches 8 patterns to find expected output text from prose descriptions (e.g., "should show", "expected output:", "verify that ... is").

### 6.4 TOON Rendering

**File:** `toon/renderer.py`

Each TOON node renders to one line in this format:
```
[s2.3] Configure BGP neighbor 192.0.2.1 | CMD: neighbor 192.0.2.1 remote-as 65001 ▸ neighbor 192.0.2.1 activate | EXPECT: BGP neighbors in Established state
```

Rollback nodes append `| [ROLLBACK]` at the end.

Prose sections are emitted as raw text without the structured format.

`render_section_only()` emits a single section — used by the context chunker when a document is split across multiple LLM calls.

### 6.5 TOON Example

Input (raw document excerpt, ~800 characters):
```
3.2 Configure BGP Neighbor
Please make sure to configure the BGP neighbor relationship to the upstream
provider. It is important to note that the neighbor IP is 192.0.2.1 with
remote-as 65001. You should also activate the neighbor under the address family.
The expected output should show the neighbor in Established state.

  neighbor 192.0.2.1 remote-as 65001
  neighbor 192.0.2.1 activate
```

TOON output (~90 characters):
```
[s3.2] Configure BGP neighbor to upstream provider | CMD: neighbor 192.0.2.1 remote-as 65001 ▸ neighbor 192.0.2.1 activate | EXPECT: neighbor in Established state
```

---

## 7. AI Layer — LLM Extraction

**Files:** `ai_layer/super_prompt_runner.py`, `ai_layer/context_chunker.py`, `ai_layer/llm_result.py`, `ai_layer/prompts/super_prompt.py`, `ai_layer/mock_llm_runner.py`

### 7.1 LLMResult Typed Wrapper

**File:** `ai_layer/llm_result.py`

Every code path that calls the LLM (or mocks it) returns an `LLMResult`. No function in the AI layer raises exceptions for LLM failures — they return `LLMResult(success=False, ...)`.

```python
class LLMErrorType(str, Enum):
    JSON_PARSE_FAIL  = "json_parse_fail"
    SCHEMA_VIOLATION = "schema_violation"
    RATE_LIMIT       = "rate_limit"
    CONTEXT_TOO_LONG = "context_too_long"
    REFUSAL          = "refusal"
    UNKNOWN          = "unknown"

@dataclass
class LLMResult:
    success:       bool
    model:         Optional[CanonicalTestModel]
    error_type:    Optional[LLMErrorType] = None
    error_message: str = ""
    raw_response:  str = ""
    latency_ms:    int = 0
    attempt_count: int = 0
    chunk_count:   int = 1
    partial_steps: List[TestStep] = field(default_factory=list)
```

### 7.2 Main Runner

**File:** `ai_layer/super_prompt_runner.py`

The runner handles two paths based on `toon_document.toon_usable`:

**TOON path:** The compressed TOON text is sent directly (usually fits in a single Claude call).

**Raw path:** The raw document text may be chunked if it exceeds the token budget.

**Retry strategy:**

```
Attempt 1: Initial call
  ├─ Success → return LLMResult(success=True)
  ├─ JSON_PARSE_FAIL → retry with JSON_CORRECTION_MESSAGE (max 3 attempts)
  ├─ SCHEMA_VIOLATION → retry with SCHEMA_CORRECTION_MESSAGE (max 3 attempts)
  ├─ RATE_LIMIT → exponential backoff [2s, 4s, 8s], then retry
  ├─ CONTEXT_TOO_LONG → non-retryable, switch to chunked mode
  └─ REFUSAL → non-retryable, return failure immediately
```

**Conversation history for retry:** On JSON/schema failures, the previous exchange (user prompt + bad LLM response + correction message) is passed back to Claude as conversation history. This gives Claude the context of what it got wrong.

**Refusal detection** (`_looks_like_refusal()`):
A response is classified as a refusal if:
1. It contains no `{` character (not a JSON response), AND
2. It contains one or more of: "I cannot", "I'm unable", "I'm not able", "I don't have", "as an AI", "I apologize"

**JSON extraction** (`_extract_json()`):
Three strategies tried in order:
1. Code fence extraction (` ```json ... ``` `)
2. First `{` to last `}` substring
3. Full response stripped

### 7.3 Context Chunker

**File:** `ai_layer/context_chunker.py`

Constants:
```python
CHARS_PER_TOKEN      = 3.5
MAX_TOKENS_PER_CHUNK = 80_000
```

Algorithm:
1. Group `DocumentBlock` objects by heading level ≤ 2 (section boundaries)
2. Greedy bin-packing: add sections to current chunk while `len(text) / 3.5 < 80_000`
3. If a single section exceeds 80k tokens, split it block-by-block with the section heading prepended to each sub-chunk

Each chunk is sent to Claude independently. Results are merged by `_merge_chunk_results()`, which renumbers all `step.sequence` values globally (1, 2, 3... across all chunks in order).

### 7.4 Prompt Design

**File:** `ai_layer/prompts/super_prompt.py`

The system prompt positions Claude as a "senior network operations engineer" and instructs it to:
- Extract all CLI commands exactly as written
- Preserve rollback steps in their own section
- Output strict JSON matching the CanonicalTestModel schema
- Not invent steps that aren't in the document

The main user prompt includes:
- Document structure detection hint from the normalizer
- Grammar engine CLI hints (list of pre-detected commands)
- The TOON-compressed or raw document text

Correction messages for retry:
- `JSON_CORRECTION_MESSAGE`: "Your previous response was not valid JSON. Please output only a valid JSON object..."
- `SCHEMA_CORRECTION_MESSAGE`: "Your response failed schema validation: {errors}. Please correct the JSON and try again..."

Two builder functions:
- `build_super_prompt(doc_text, grammar_hints, structure)` — single-document prompt
- `build_chunk_prompt(chunk_text, chunk_index, total_chunks, grammar_hints)` — chunked prompt includes chunk position context

### 7.5 Mock LLM Runner

**File:** `ai_layer/mock_llm_runner.py`

For offline testing and CI without an Anthropic API key. Builds a `CanonicalTestModel` directly from grammar-engine detected commands using heuristic section assignment.

**Section assignment heuristic (`_guess_section()`):**

| Condition | Section | StepType | ActionType |
|-----------|---------|----------|------------|
| Rollback keywords in command | Rollback | ROLLBACK | ROLLBACK |
| First 20% of commands + pre-check keywords | Pre-checks | VERIFICATION | VERIFY |
| Last 25% of commands + verify keywords | Verification | VERIFICATION | VERIFY |
| "show" command + position < 40% | Pre-checks | VERIFICATION | VERIFY |
| "show" command + position ≥ 40% | Verification | VERIFICATION | VERIFY |
| Default | Implementation | ACTION | EXECUTE |

**Section-aware deduplication:**

A critical design: the same CLI command is legitimate in both Pre-checks and Verification (e.g., `show bgp summary` as a baseline AND as a post-change check). The deduplication only removes a command if the exact same command has already appeared in the **same section**:

```python
seen_per_section: dict[str, set[str]] = {}
for idx0, c in enumerate(detected_commands, start=1):
    section0, _, _ = _guess_section(c.raw, idx0, total)
    key = c.normalized or c.raw.lower().strip()
    if section0 not in seen_per_section:
        seen_per_section[section0] = set()
    if key not in seen_per_section[section0]:
        seen_per_section[section0].add(key)
        unique_cmds.append(c)
```

Global deduplication (previous implementation) was wrong because it would discard the post-check `show bgp summary`, leaving the procedure without a verification step.

---

## 8. Post-Processing — Guardrails and Schema Validation

**Files:** `post_processing/guardrails.py`, `post_processing/schema_validator.py`

### 8.1 Guardrails

**File:** `post_processing/guardrails.py`

Six checks are run on the `CanonicalTestModel` after LLM extraction:

| Check | Severity | Description |
|-------|----------|-------------|
| `has_steps` | ERROR | Model must have at least one step |
| `step_descriptions` | WARN | Each step description should be ≥5 chars |
| `no_duplicate_sequences` | WARN | No two steps should share the same sequence number |
| `empty_commands` | WARN | Steps of type ACTION/VERIFICATION should have ≥1 command |
| `rollback_consistency` | WARN + auto-correct | Steps tagged rollback in description but not flagged `is_rollback=True` are auto-corrected |
| `command_coverage` | WARN | Post-LLM command count should be ≥50% of pre-LLM grammar count |

```python
@dataclass
class GuardrailResult:
    passed:         bool
    warnings:       List[str]
    errors:         List[str]
    coverage_ratio: float
```

Only `has_steps` failing causes the pipeline to abort. All other failures are warnings that are logged but allow the pipeline to continue.

The `--skip-guardrails` flag bypasses all checks (useful for testing with intentionally minimal documents).

### 8.2 Schema Validator

**File:** `post_processing/schema_validator.py`

After guardrails, a Pydantic round-trip validation is performed:
```python
model_validate_json(model.model_dump_json())
```

This catches any inconsistency introduced during LLM extraction where a value might appear valid in one representation but fail on strict parse.

Additional business-rule checks:
- Step sequences are in ascending order (warns if not, does not reorder)
- `document_title` is non-empty
- `source_format` is one of: `{pdf, docx, txt}`

`to_json()` serializes the validated model:
```python
def to_json(model: CanonicalTestModel) -> str:
    return model.model_dump_json(indent=2)
```

---

## 9. Generators — Output Artifacts

### 9.1 Zephyr Scale CSV Generator

**File:** `generators/zephyr_generator.py`

Produces a CSV ready for Zephyr Scale bulk import. Each `TestStep` becomes one row.

**CSV columns:**

| Column | Source |
|--------|--------|
| Name | `Step_{sequence:03d}: {description[:80]}` |
| Objective | `step.description` |
| Precondition | Commands joined with `; ` |
| Status | "Draft" |
| Priority | See priority map below |
| Labels | `step.tags` joined |
| Component | `step.section` |
| Folder | `/MOPs/{safe_title}/{section}` |
| Steps | JSON array of step objects |

**Priority mapping:**

| StepType | Priority |
|----------|----------|
| ROLLBACK | Critical |
| VERIFICATION | High |
| ACTION | Medium |
| INFO | Low |

**Steps column format** (JSON array):
```json
[
  {
    "step": "Execute: show bgp summary",
    "data": "Command: show bgp summary",
    "result": "BGP neighbors in Established state"
  }
]
```

### 9.2 Robot Framework Generator

**File:** `generators/robot_generator.py`

Produces a complete, executable `.robot` file. The generator is comprehensive — it handles all failure strategies, pre-check baseline capture, rollback keyword isolation, and error detection.

**Settings section:**

For `ROLLBACK_ALL` / `ROLLBACK_GROUP` strategy with rollback steps present:
```robot
Suite Teardown   Run Keywords    Run Keyword If Any Tests Failed    Execute Rollback Procedure
...              AND    Close All Connections
```

For all other cases:
```robot
Suite Teardown   Close All Connections
```

**Test case structure:**

Each non-rollback step becomes a test case:
```robot
Step_001: Configure BGP neighbor 192.0.2.1
    [Documentation]    Configure BGP neighbor to upstream provider
    [Tags]    action    bgp    cisco
    [Teardown]    Run Keyword If Test Failed    Log Step Failure    Configure BGP neighbor
    ${neighbor_192}=    Execute CLI Command    neighbor 192.0.2.1 remote-as 65001
    Verify No Device Error    ${neighbor_192}
```

**Strategy-specific behavior:**

| Strategy | Effect |
|----------|--------|
| ABORT (default) | No special tags; first failure stops the suite |
| CONTINUE | Adds `robot:continue-on-failure` tag to every non-rollback step |
| ROLLBACK_ALL | Suite Teardown calls Execute Rollback Procedure on any failure |
| ROLLBACK_GROUP | Same as ROLLBACK_ALL |

**Pre-check steps** save output to suite variables for post-check comparison:
```robot
${BASELINE_SHOW_BGP}=    Execute CLI Command    show bgp summary
Set Suite Variable    ${BASELINE_SHOW_BGP}
```

**Verify No Device Error** regex (catches all common device error patterns):
```
(?i)(^\s*%|\berror\b|\binvalid\b|\bfailed\b|\btimed?\s*out\b|permission denied|syntax error|not permitted)
```

**Execute Rollback Procedure** keyword runs rollback steps in **reverse order**:
```python
for step in reversed(rollback_steps):
    for cmd in step.commands:
        lines.append(f"Run Keyword And Continue On Failure    Execute CLI Command    {cmd.raw}")
```

**Generated keywords:**
1. `Open SSH Connection` — connects via SSHLibrary
2. `Execute CLI Command` — executes and returns output
3. `Verify Command Output` — asserts expected text in output
4. `Verify No Device Error` — asserts no error patterns
5. `Log Step Failure` — logs context when a step fails
6. `Execute Rollback Procedure` — runs rollback steps in reverse (only if rollback steps exist)

### 9.3 CLI Rule Generator

**File:** `generators/cli_rule_generator.py`

Produces a JSON file of per-command validation rules. This can be consumed by external tooling (Ansible, custom scripts) for independent validation.

Each command in each step becomes one rule object:
```json
{
  "step_sequence": 3,
  "step_id": "a1b2c3d4",
  "step_type": "action",
  "section": "Implementation",
  "command": "neighbor 192.0.2.1 remote-as 65001",
  "vendor": "cisco",
  "protocol": "bgp",
  "mode": "config",
  "confidence": 0.85,
  "is_rollback": false,
  "must_contain": ["neighbor 192.0.2.1 in state Established"],
  "must_not_contain": ["Error", "Invalid input", "%"],
  "tags": ["bgp", "cisco"],
  "description": "Configure BGP neighbor 192.0.2.1 remote-as 65001"
}
```

`must_contain` is populated from `step.expected_output` (split by newlines/semicolons). `must_not_contain` always includes the three standard error indicators.

---

## 10. Phase 2 — Execution Engine Architecture

**Directory:** `execution_engine/`

Phase 2 takes the `CanonicalTestModel` JSON from Phase 1 and executes it against real network devices via SSH. The architecture is a pipeline of four sequential agents, each with a well-defined responsibility.

### 10.1 Agent Responsibilities

```
PlannerAgent
  ├─ Validates the DAG (dependency graph)
  ├─ Computes execution waves
  ├─ Identifies critical path
  ├─ Groups transaction steps
  ├─ Checks for blocked commands
  ├─ Determines if approval is required
  └─ Produces ExecutionPlan

ExecutionAgent
  ├─ Iterates waves (parallel within a wave)
  ├─ Checks kill switch before each step
  ├─ Handles per-step and per-execution approval gates
  ├─ Checks maintenance window
  ├─ Checks idempotency (SKIP verdict → skip without executing)
  ├─ Executes via DeviceDriver
  ├─ Calls ValidationAgent on output
  ├─ Calls RecoveryAgent on failure
  └─ Updates ExecutionState after every step

ValidationAgent
  ├─ Passive: checks expected_output on VERIFY/OBSERVE steps
  ├─ Active: runs validation_rules commands and checks their output
  └─ Returns combined (passed, errors) tuple

RecoveryAgent
  ├─ make_decision(): RETRY / ROLLBACK / CONTINUE / ESCALATE / SKIP
  ├─ Detects fatal errors (auth, no route, connection refused) → ESCALATE
  ├─ Writes decisions to output/decision.log (one JSON per line)
  ├─ rollback_group(): rollback from last savepoint
  └─ rollback_all(): rollback all PASSED steps in reverse sequence
```

### 10.2 Execution State Machine

```
          ┌──────────┐
          │  PENDING │ ← ExecutionState created
          └────┬─────┘
               │ PlannerAgent.plan()
               ▼
          ┌──────────┐
          │ RUNNING  │ ← ExecutionAgent wave loop
          └──┬───┬───┘
             │   │
    success  │   │  failure
             │   │
             ▼   ▼
        PASSED   FAILED ──→ ROLLED_BACK (if rollback succeeds)
             │
         ABORTED (kill switch / API abort)
             │
         PAUSED ──→ RUNNING (on resume)
```

Per-step state machine mirrors this: each `StepResult.status` independently transitions through `PENDING → RUNNING → PASSED/FAILED/SKIPPED/ABORTED`.

---

## 11. DAG Engine — Dependency and Wave Planning

**File:** `execution_engine/dag_engine.py`

The DAG engine is a pure Python implementation of Kahn's topological sort algorithm. No external graph libraries (networkx, etc.) are used.

### 11.1 Wave Generation (Kahn's Algorithm)

```python
def compute_waves(self) -> List[List[str]]:
    in_degree = {n: 0 for n in self.nodes}
    for node in self.nodes:
        for dep in self.deps.get(node, []):
            in_degree[node] += 1

    queue = deque(n for n in self.nodes if in_degree[n] == 0)
    waves = []

    while queue:
        wave = sorted(queue)  # sorted for determinism
        waves.append(wave)
        next_queue = deque()
        for node in wave:
            for successor in self.successors.get(node, []):
                in_degree[successor] -= 1
                if in_degree[successor] == 0:
                    next_queue.append(successor)
        queue = next_queue

    return waves
```

Steps in the same wave have no dependencies on each other and can execute in parallel (up to the concurrency limit).

### 11.2 Cycle Detection (DFS)

```python
WHITE, GRAY, BLACK = 0, 1, 2

def has_cycle(self) -> bool:
    color = {n: WHITE for n in self.nodes}

    def dfs(node):
        color[node] = GRAY
        for dep in self.deps.get(node, []):
            if color[dep] == GRAY:
                return True  # Back edge → cycle
            if color[dep] == WHITE and dfs(dep):
                return True
        color[node] = BLACK
        return False

    return any(dfs(n) for n in self.nodes if color[n] == WHITE)
```

A cycle in the dependency graph is an error that prevents plan generation.

### 11.3 Critical Path

Computed via dynamic programming along the longest path:

```python
def critical_path(self) -> List[str]:
    dp = {n: (1, None) for n in self.nodes}  # (length, predecessor)
    for node in topological_order:
        for successor in self.successors.get(node, []):
            if dp[node][0] + 1 > dp[successor][0]:
                dp[successor] = (dp[node][0] + 1, node)
    # Trace back from max-length node
```

### 11.4 Ready Steps

During execution, the engine can query which steps are ready to run given a set of completed steps:

```python
def ready_steps(self, completed: Set[str]) -> List[str]:
    return [
        n for n in self.nodes
        if n not in completed
        and all(dep in completed for dep in self.deps.get(n, []))
    ]
```

### 11.5 Estimated Duration

```python
def estimated_duration_s(self) -> float:
    path = self.critical_path()
    total = 0.0
    for step_id in path:
        step = self._step_map[step_id]
        total += step.execution_policy.timeout_s
        total += step.timing.delay_before_s
        total += step.timing.delay_after_s
    return total
```

---

## 12. State Manager — Execution Persistence

**File:** `execution_engine/state_manager.py`

The state manager is the single source of truth for all execution state. It is file-backed for durability across process restarts.

### 12.1 File Layout

```
output/
├── executions/
│   ├── {execution_id}.json      # ExecutionState serialized to JSON
│   ├── locks/
│   │   └── {execution_id}.lock  # Lock file prevents concurrent execution of same ID
│   └── archive/
│       └── {execution_id}.json  # TTL-expired states moved here
```

Constants:
```python
_EXEC_DIR    = "output/executions"
_LOCK_DIR    = "output/executions/locks"
_ARCHIVE_DIR = "output/executions/archive"
```

### 12.2 Thread Safety

Each `execution_id` gets its own `threading.RLock` in `_locks: Dict[str, RLock]`. The `_meta_lock` (`threading.Lock`) protects the `_locks` dictionary itself against concurrent creation of new keys.

### 12.3 Atomic Write

State is written atomically using rename semantics:
```python
tmp = path.with_suffix(".tmp")
tmp.write_text(state.model_dump_json(indent=2))
tmp.replace(path)   # Atomic on POSIX
```

This guarantees that a reader never sees a partially-written state file.

### 12.4 TTL Sweep

A daemon thread runs hourly and archives state files older than 168 hours (7 days):
```python
# Started in __init__:
t = threading.Thread(target=self._ttl_sweep, daemon=True)
t.start()
```

Archived files are moved to `_ARCHIVE_DIR` rather than deleted.

### 12.5 Key Operations

| Method | Description |
|--------|-------------|
| `create(model, dry_run)` | Allocates execution_id (UUID4), creates state, returns id |
| `get(execution_id)` | Returns `ExecutionState`; raises `KeyError` if not found |
| `transition_execution(id, status, agent, message)` | Adds `TransitionRecord` to history, updates status |
| `update_step(id, step_id, result)` | Updates one `StepResult` in the steps dict |
| `update_field(id, **kwargs)` | Updates arbitrary scalar fields (paused, itsm_updated, etc.) |
| `request_kill(execution_id=None)` | Sets `kill_requested=True` on one or all running executions |
| `set_approval(id, approver_id, decision)` | Records approval decision |
| `list_executions(limit)` | Returns list of summary dicts |

### 12.6 Module-Level Singleton

```python
state_manager = StateManager()
```

Imported everywhere as `from execution_engine.state_manager import state_manager`.

---

## 13. Sequential Agents — Planner, Executor, Validator, Recovery

### 13.1 Planner Agent

**File:** `execution_engine/planner_agent.py`

`PlannerAgent.plan(model, dry_run)` → `ExecutionPlan`

Steps:
1. Build a `DAGEngine` from `step.depends_on` edges
2. Call `dag.has_cycle()` — abort if True
3. Call `dag.compute_waves()` → wave list
4. Call `dag.critical_path()` → step_id list
5. Compute `transaction_groups`: collect all steps sharing the same `transaction_group` string; only create a group entry if ≥2 steps share it
6. Build `device_list` from all `step.devices[].hostname`
7. Check approval: `model.approval_required OR any step.approval_required OR blast_radius in (HIGH, CRITICAL)`
8. Check `blocked_commands` from `execution_defaults.yaml`; if matched, either raise or set `approval_required=True` based on blast radius
9. If `dry_run=True`, print ASCII plan summary via `_print_dry_run_summary()`

```python
@dataclass
class ExecutionPlan:
    waves:              List[List[str]]     # List of waves; each wave = list of step_ids
    transaction_groups: Dict[str, List[str]]
    critical_path:      List[str]
    requires_approval:  bool
    approval_reasons:   List[str]
    device_list:        List[str]
    estimated_duration_s: float
```

### 13.2 Execution Agent

**File:** `execution_engine/execution_agent.py`

`ExecutionAgent.run(execution_id)` drives the full execution lifecycle.

**Wave loop:**
```python
for wave in plan.waves:
    with ThreadPoolExecutor(max_workers=min(10, len(wave))) as pool:
        futures = {pool.submit(self._execute_step, execution_id, step_id): step_id
                   for step_id in wave}
        for future in as_completed(futures):
            result = future.result()  # StepResult
```

**Per-step flow:**
```
_execute_step(execution_id, step_id):
  1. Kill switch check → abort if set
  2. Per-step approval gate (_wait_for_step_approval, polls every 5s up to 1800s)
  3. Maintenance window check
  4. Delay before (step.timing.delay_before_s)
  5. Get driver (connection_pool.acquire(hostname, creds))
  6. Idempotency check → if SKIP, record skipped_reason, return
  7. transition_execution RUNNING
  8. Retry loop:
     a. driver.execute(cmd) → output
     b. validation_agent.validate(step, output) → (passed, errors)
     c. If passed → break
     d. If not passed → recovery_agent.make_decision()
        - RETRY: sleep retry_delay_s, loop again
        - ROLLBACK: call _decide_on_failure(ROLLBACK)
        - ESCALATE: call _decide_on_failure(ESCALATE)
        - CONTINUE: log warning, break
  9. transition_execution PASSED or FAILED
 10. Delay after (step.timing.delay_after_s)
```

**Approval polling:**
```python
def _wait_for_approval(self, execution_id: str, timeout_s=3600):
    """Block until approval granted or denied; poll every 5s."""

def _wait_for_step_approval(self, execution_id: str, step_id: str, timeout_s=1800):
    """Block until per-step approval granted; poll every 5s."""
```

**`_decide_on_failure`** routes to `RecoveryAgent` and then either:
- Continues execution (CONTINUE strategy)
- Calls `RecoveryAgent.rollback_all()` or `rollback_group()` (ROLLBACK strategies)
- Aborts the execution (ABORT strategy)

### 13.3 Validation Agent

**File:** `execution_engine/validation_agent.py`

Stateless. Called per step after each command execution.

```python
_ERROR_PATTERNS = re.compile(
    r"(?i)(^\s*%|\berror\b|\binvalid\b|\bfailed\b|\btimed?\s*out\b|"
    r"permission denied|syntax error|not permitted)"
)

def validate(self, step: TestStep, output: str) -> tuple[bool, List[str]]:
    errors = []

    # Passive: expected_output match for VERIFY/OBSERVE steps only
    if step.action_type in (ActionType.VERIFY, ActionType.OBSERVE):
        if step.expected_output:
            if not self._match_expected(output, step.expected_output):
                errors.append(f"Expected '{step.expected_output}' not found")

    # Active: run validation_rules
    for rule in step.validation_rules:
        rule_output = self.driver.execute(rule.command)
        matched = bool(re.search(rule.expect_pattern, rule_output, re.IGNORECASE))
        if rule.negate and matched:
            errors.append(f"Rule '{rule.expect_pattern}' matched but should not have")
        elif not rule.negate and not matched:
            errors.append(f"Rule '{rule.expect_pattern}' did not match")

    return len(errors) == 0, errors
```

`_match_expected()` first tries `re.search(expected, output, IGNORECASE)`, then falls back to case-insensitive substring check. This handles both regex and plain-text expected outputs.

### 13.4 Recovery Agent

**File:** `execution_engine/recovery_agent.py`

**Decision logic (`make_decision`):**

```
If attempts < max_retry_count:
    → RETRY

If fatal error in output:
    (permission denied | auth failed | no route to host |
     connection refused | host key mismatch)
    → ESCALATE

Based on failure_strategy:
    ABORT          → ABORT
    CONTINUE       → CONTINUE
    ROLLBACK_ALL   → ROLLBACK
    ROLLBACK_GROUP → ROLLBACK
```

**Fatal error detection** triggers ESCALATE regardless of retry count. These errors indicate the device cannot be reached or authenticated, so retrying is pointless and may cause further damage.

**Decision log:** Every decision is written as a single JSON line to `output/decision.log`:
```json
{"timestamp": "2026-03-25T10:30:00", "execution_id": "abc123", "step_id": "d4e5f6", "decision": "RETRY", "attempt": 2, "reason": "Validation failed: expected 'Established' not found"}
```

**Vendor-aware rollback command generation (`_build_rollback_commands`):**

| Vendor | Rollback pattern |
|--------|-----------------|
| cisco, arista, nokia, ericsson | `no <original_command>` |
| juniper | `delete <statement>` or `deactivate <statement>` |
| huawei | `undo <original_command>` |
| exec-mode commands | Skipped (cannot be negated) |

**`rollback_group(execution_id, model, from_step_id)`:**
Finds the highest-sequence PASSED step with `savepoint=True` before `from_step_id`. Rolls back all PASSED steps from `from_step_id` back to the savepoint.

**`rollback_all(execution_id, model, context)`:**
Collects all steps with `StepResult.status == PASSED`, sorts by `sequence` descending, and executes rollback commands in that order.

---

## 14. Device Layer — Drivers, Credentials, Connection Pool

### 14.1 Device Drivers

**File:** `device_layer/device_driver.py`

Abstract base class:
```python
class DeviceDriver(ABC):
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def execute(self, command: str, timeout_s: float = 30.0) -> str: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...
```

**MockDriver:**
Used in `dry_run=True` mode and all unit tests. Returns pattern-matched responses:
- Commands containing "show" → formatted show output
- Commands containing "error" → simulated error output
- All others → `"OK"` with a configurable `simulate_delay_s=0.05` sleep

**NetmikoDriver:**
Wraps Netmiko's `ConnectHandler`. Vendor-to-device-type mapping:

```python
_VENDOR_TO_DEVICE_TYPE = {
    "cisco":     "cisco_xr",
    "juniper":   "juniper_junos",
    "nokia":     "nokia_sros",
    "arista":    "arista_eos",
    "huawei":    "huawei_vrp",
    "f5":        "linux",
    "palo_alto": "paloalto_panos",
    "checkpoint": "checkpoint_gaia",
    "generic":   "cisco_ios",
}
```

Session logging: each connection's I/O is logged to `output/sessions/{hostname}_{timestamp}.log`.

Jump host support via environment variables:
- `JUMP_HOST` — global jump host
- `JUMP_HOST_{HOSTNAME}` — per-device override

Jump connections use `paramiko.Transport` to open a forwarded socket, which is passed as the `sock` parameter to Netmiko's `ConnectHandler`.

### 14.2 Connection Pool

**File:** `device_layer/connection_pool.py`

Pool key: `(hostname, username)` tuple.

**`acquire(hostname, username, password, vendor)`:**
1. Check `_pool[(hostname, username)]` for an existing driver
2. If found and `is_connected` → reset idle timer, return driver
3. If not found or disconnected → create new `NetmikoDriver`, connect, store in pool

**Idle sweep daemon** (runs every 60 seconds):
```python
# Close connections idle for > idle_timeout_s (default: 600s)
for key, entry in list(self._pool.items()):
    if time.time() - entry.last_used > self._idle_timeout_s:
        entry.driver.disconnect()
        del self._pool[key]
```

Module-level singleton: `connection_pool = ConnectionPool()`.

### 14.3 Credential Store

**File:** `device_layer/credential_store.py`

Resolution order (first match wins):

```
1. HashiCorp Vault (if hvac installed and VAULT_ADDR + VAULT_TOKEN set)
   Path: network/{hostname}
   Fields: username, password, enable_password

2. Per-host environment variables:
   DEVICE_CREDS_{HOSTNAME_UPPER}_USER
   DEVICE_CREDS_{HOSTNAME_UPPER}_PASS
   DEVICE_CREDS_{HOSTNAME_UPPER}_ENABLE

3. Default environment variables:
   DEVICE_DEFAULT_USER
   DEVICE_DEFAULT_PASS
   DEVICE_DEFAULT_ENABLE

4. AES-256-GCM encrypted .credentials.json
   Key: CREDS_KEY environment variable (base64-encoded 32-byte key)
   Format: {hostname: {username: ..., password: ..., enable_password: ...}}
```

Never returns `None` — raises `CredentialNotFoundError` if no credentials found at any level.

Module-level singleton: `credential_store = CredentialStore()`.

---

## 15. Safety Systems — Kill Switch, RBAC, Maintenance Window

### 15.1 Kill Switch

**File:** `execution_engine/kill_switch.py`

Two-layer design for defense in depth:
1. `threading.Event` — fast in-process check (`is_set()` is O(1))
2. File sentinel `output/kill_switch.flag` — survives process restarts

On startup, the `KillSwitch.__init__()` checks for the sentinel file and re-engages the event if present, ensuring a kill switch engaged before restart stays engaged after restart.

```python
class KillSwitch:
    def engage(self, reason: str = ""):
        self._event.set()
        Path("output/kill_switch.flag").write_text(reason or "engaged")

    def clear(self):
        self._event.clear()
        Path("output/kill_switch.flag").unlink(missing_ok=True)

    def is_set(self) -> bool:
        return self._event.is_set()
```

Module-level singleton: `kill_switch = KillSwitch()`.

The kill switch is checked before every individual step in the execution agent wave loop. A kill does not roll back automatically — it stops execution immediately and leaves the system in its current state.

### 15.2 RBAC

**File:** `safety/rbac.py`

Four roles with numeric levels:

| Role | Level | Can Do |
|------|-------|--------|
| reader | 0 | GET status, reports, metrics |
| executor | 1 | Start/pause/resume/abort executions |
| approver | 2 | Submit approval decisions |
| admin | 3 | Engage/clear kill switch |

Implementation: `require_role(min_role)` returns a FastAPI dependency. The dependency reads the `X-Api-Key` header, looks up the key in `configs/rbac.yaml`, and compares the role level.

Dev mode: if `configs/rbac.yaml` is empty or missing, all requests are treated as admin. This allows running locally without setting up API keys.

API key environment variable: `MOP_API_KEY` (in `api/auth.py`). If unset → dev mode.

Config file format:
```yaml
api_keys:
  mykey123: admin
  readonlykey: reader
approval_quorum: 1
```

### 15.3 Maintenance Window

**File:** `safety/maintenance_window.py`

```python
def check_window(window_start: str, window_end: str) -> None:
    """Raise MaintenanceWindowError if current time is outside [start, end]."""

def wait_for_window(window_start: str, window_end: str, grace_period_s: int = 1800) -> None:
    """Poll every 60s up to grace_period_s waiting for window to open.
    Raises MaintenanceWindowError if window doesn't open in time."""
```

Window times are ISO format strings. The execution agent calls `check_window()` before the first wave and optionally calls `wait_for_window()` during the pre-execution setup phase.

### 15.4 Concurrency Controller

**File:** `execution_engine/concurrency_controller.py`

Two-level locking:
1. `BoundedSemaphore(max_devices)` — global across all devices (default: 5)
2. `Dict[hostname, Lock]` — per-device mutex preventing two steps on the same device from running simultaneously

```python
@contextmanager
def acquire_device(self, hostname: str, timeout_s: float = 300.0):
    """Acquire global semaphore + per-device lock within timeout."""
    acquired = self._global_sem.acquire(timeout=timeout_s)
    if not acquired:
        raise RuntimeError(f"Could not acquire device slot within {timeout_s}s")
    try:
        device_lock = self._get_device_lock(hostname)
        device_lock.acquire(timeout=timeout_s)
        try:
            yield
        finally:
            device_lock.release()
    finally:
        self._global_sem.release()
```

Limits loaded from `configs/execution_defaults.yaml`:
```yaml
limits:
  max_concurrent_devices: 5
  max_concurrent_steps_per_device: 1
```

Module-level singleton: `concurrency_controller = ConcurrencyController()`.

---

## 16. Smart Wait — Polling Engine and Idempotency

### 16.1 Polling Engine

**File:** `smart_wait/polling_engine.py`

Implements exponential backoff polling for convergence-based network states.

```python
@dataclass
class PollingResult:
    success:      bool
    elapsed_s:    float
    attempts:     int
    last_output:  str
    matched_at:   Optional[int]   # Attempt number where pattern matched

def wait_for(
    driver: DeviceDriver,
    command: str,
    pattern: str,
    timeout_s: float = 300.0,
    negate: bool = False,
) -> PollingResult:
```

Backoff schedule: `5s → 10s → 20s → 40s → 60s` (capped at 60s).

The `negate=True` mode waits for the pattern to **disappear** — useful for "wait until route is withdrawn" or "wait until interface goes down".

Used by the execution agent for steps with `step.timing.max_wait_s > 0`.

### 16.2 Idempotency Engine

**File:** `smart_wait/idempotency_engine.py`

Prevents executing commands that have already been applied. Before each step, if an `idempotency_rule` is set, the check command is run and the output is matched against `skip_if_pattern` and `skip_patterns`.

```python
class IdempotencyVerdict(str, Enum):
    PROCEED       = "proceed"       # Not already applied, proceed normally
    SKIP          = "skip"          # Fully applied, skip this step
    PARTIAL_STATE = "partial_state" # Some but not all changes applied

_NON_IDEMPOTENT_PREFIXES = (
    "undo ", "no ", "delete ", "reload", "erase",
    "shutdown", "format "
)
```

Commands starting with non-idempotent prefixes always return `PROCEED` (these commands are destructive — the check would be meaningless).

**Partial state detection:** If `len(skip_patterns) > 1`, the engine runs each pattern individually. If >0 but <100% of patterns match, the verdict is `PARTIAL_STATE` (logged as a warning, execution proceeds).

---

## 17. Notifications

**Files:** `notifications/notification_router.py`, `notifications/slack_notifier.py`, `notifications/email_notifier.py`, `notifications/pagerduty_notifier.py`

### 17.1 Event Types

Eleven events are routed through the notification system:

| Event | Slack | Email | PagerDuty |
|-------|-------|-------|-----------|
| `execution_started` | Yes | No | No |
| `approval_required` | Yes | Yes | No |
| `step_failed_with_retry` | Yes | No | No |
| `step_failed_no_retry` | Yes | No | No |
| `execution_passed` | Yes | Yes | Resolve |
| `execution_failed` | Yes | Yes | Trigger |
| `rollback_started` | Yes | No | No |
| `rollback_passed` | Yes | No | Resolve |
| `rollback_failed` | Yes | No | Trigger |
| `kill_switch_engaged` | Yes | Yes | Trigger |
| `maintenance_window_expiring` | Yes | No | No |

### 17.2 Notification Router

**File:** `notifications/notification_router.py`

All notifier exceptions are caught independently — a Slack failure does not prevent the email from being sent:

```python
def _dispatch(self, event: str, **kwargs):
    for notifier in self._get_notifiers_for_event(event):
        try:
            notifier.send(event, **kwargs)
        except Exception as exc:
            logger.warning("Notifier %s failed for %s: %s", notifier, event, exc)
```

Event-to-notifier routing is configured in `configs/notifications.yaml`. Each notifier has an `enabled` flag and a list of subscribed events.

Module-level singleton: `notification_router = NotificationRouter()`.

### 17.3 Slack Notifier

**File:** `notifications/slack_notifier.py`

Configuration: `SLACK_WEBHOOK_URL` environment variable.

Color coding per event:
- `execution_passed`, `rollback_passed` → green
- `execution_failed`, `rollback_failed`, `kill_switch_engaged` → red
- `approval_required`, `step_failed_with_retry` → yellow
- All others → blue

Dry-run mode logs `[NOTIFICATION_DRY_RUN]` instead of sending.

### 17.4 Email Notifier

**File:** `notifications/email_notifier.py`

Configuration: `SMTP_HOST`, `SMTP_USER`, `SMTP_PASS`, `SMTP_PORT` (default 587), `NOTIFY_EMAIL_TO`.

Only 5 important events trigger email (to avoid inbox noise):
`execution_failed`, `rollback_failed`, `kill_switch_engaged`, `execution_passed`, `approval_required`.

Sends HTML-formatted email via `aiosmtplib` (or `smtplib` synchronously).

### 17.5 PagerDuty Notifier

**File:** `notifications/pagerduty_notifier.py`

Configuration: `PD_INTEGRATION_KEY`.

Uses PagerDuty Events API v2.

Dedup key: `mop-{execution_id}` — ensures all events for an execution correlate to one PD incident.

- **Trigger** events: `execution_failed`, `rollback_failed`, `kill_switch_engaged`
- **Resolve** events: `execution_passed`, `rollback_passed`

---

## 18. ITSM Integration

**Files:** `itsm/itsm_client.py`, `itsm/servicenow_adapter.py`, `itsm/jira_adapter.py`

### 18.1 ITSM Client Facade

**File:** `itsm/itsm_client.py`

Routes all ITSM operations to the correct adapter based on `CanonicalTestModel.change_ticket.system`:

```python
class ITSMClient:
    def comment(self, ticket: ITSMRef, body: str) -> None: ...
    def transition(self, ticket: ITSMRef, to_state: str) -> None: ...
    def notify_execution_started(self, ticket, execution_id) -> None: ...
    def notify_step_failed(self, ticket, execution_id, step_id, error) -> None: ...
    def notify_execution_passed(self, ticket, execution_id, duration_s) -> None: ...
    def notify_execution_failed(self, ticket, execution_id, failed_steps) -> None: ...
    def notify_rollback_completed(self, ticket, execution_id) -> None: ...
```

All methods catch exceptions to prevent ITSM failures from affecting execution.

After execution completes (in the `api/execution_routes.py` background thread), if `state.canonical_model.change_ticket` is set, the ITSM client is called automatically.

### 18.2 ServiceNow Adapter

**File:** `itsm/servicenow_adapter.py`

Authentication: `ITSM_USERNAME` + `ITSM_PASSWORD` (Basic auth).

State map for `transition()`:

| State name | ServiceNow state code |
|------------|----------------------|
| In Progress | 2 |
| Implemented | 3 |
| Failed | -1 |
| Closed | 7 |

Update endpoint: `PATCH /api/now/table/change_request/{sys_id}`

Comments are added via `PATCH` to the `work_notes` field.

### 18.3 Jira Adapter

**File:** `itsm/jira_adapter.py`

Authentication: `ITSM_USERNAME` + `ITSM_TOKEN` (Basic auth with API token).

Comments use Jira's Atlassian Document Format (ADF):
```json
{
  "body": {
    "version": 1,
    "type": "doc",
    "content": [
      {
        "type": "paragraph",
        "content": [{"type": "text", "text": "Execution passed..."}]
      }
    ]
  }
}
```

Transitions are dynamic — the adapter first calls `GET /rest/api/3/issue/{key}/transitions` to get the current list, then matches the desired state name case-insensitively, and POSTs the `transition_id`.

---

## 19. Reporting

**File:** `reporting/execution_report.py`

### 19.1 Report Structure

```python
@dataclass
class ExecutionReport:
    execution_id:       str
    status:             str
    document_title:     str
    started_at:         str
    completed_at:       str
    total_steps:        int
    passed_steps:       int
    failed_steps:       int
    skipped_steps:      int
    dry_run:            bool
    per_step:           List[Dict]          # One dict per step
    per_device_summary: Dict[str, Dict]     # One dict per device
    timeline:           List[Dict]          # Transition history
    decision_log_summary: List[Dict]        # From output/decision.log
```

**Per-step fields:**
- `step_id`, `sequence`, `section`, `description`
- `device` (hostname or "dry_run")
- `status` (passed/failed/skipped/aborted)
- `commands_executed` (list of command strings)
- `actual_output_snippet` (first 300 characters of output)
- `duration_s`, `retries`, `validation_errors`

**Per-device summary:**
- `steps_run`, `steps_passed`, `steps_failed`
- `avg_duration_s`

### 19.2 HTML Rendering

`render_html()` tries Jinja2 first with the template at `reporting/templates/report.html`. If Jinja2 is unavailable, it falls back to inline Python string formatting of an HTML structure.

### 19.3 Report Persistence

`save(execution_id)` writes two files:
```
output/reports/{execution_id}_report.json
output/reports/{execution_id}_report.html
```

These are written by the background thread in `api/execution_routes.py` after execution completes (in the `finally` block, so they always run even on failure).

### 19.4 Decision Log Integration

`output/decision.log` is read (if present) and filtered for the current `execution_id`. The resulting decisions are summarized in `decision_log_summary`:
```json
[
  {"timestamp": "...", "step_id": "...", "decision": "RETRY", "attempt": 2, "reason": "..."},
  {"timestamp": "...", "step_id": "...", "decision": "ROLLBACK", "attempt": 3, "reason": "..."}
]
```

---

## 20. REST API — Phase 1 and Phase 2 Endpoints

### 20.1 FastAPI Application

**File:** `api/main.py`

The FastAPI application is initialized with a `lifespan` context manager that:
1. Configures logging (rotating file handler + console handler)
2. Creates output directories (`output/jobs`, `output/uploads`, `logs`)
3. Creates a `ThreadPoolExecutor` for Phase 1 pipeline jobs (`MAX_WORKERS` env, default 3)
4. Yields (application running)
5. Shuts down the executor

Request logging middleware (runs on every request):
- Assigns a UUID4 request ID (first 8 chars)
- Logs method, path, client IP at DEBUG level
- Logs status code and elapsed milliseconds at INFO/WARNING level
- Adds `X-Request-ID` response header

CORS: `allow_origins=["*"]` — open for development. Restrict in production.

Routers:
- `router` from `api/routes.py` mounted at `/api/v1`
- `execution_router` from `api/execution_routes.py` mounted at `/api/v2`

Static files served from `./static/` directory (web UI at `/`).

Health endpoint: `GET /health` returns `{status: "ok", version: "1.0.0", kill_switch: bool}`.

### 20.2 Phase 1 Endpoints

**File:** `api/routes.py` (prefix: `/api/v1`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/process` | Upload document, start async pipeline job |
| GET | `/status/{job_id}` | Get job status and progress message |
| GET | `/result/{job_id}` | Get full job result including canonical model |
| GET | `/download/{job_id}/{artifact}` | Download artifact file |
| GET | `/jobs` | List all jobs (most recent first) |
| GET | `/logs/{job_id}` | Get per-job log lines |

**Artifact names for download:**
- `zephyr` → `{title}_zephyr.csv`
- `robot` → `{title}.robot`
- `cli_rules` → `{title}_cli_rules.json`
- `canonical` → `{title}_canonical.json`

**Job lifecycle:**
```
POST /process:
  1. Save uploaded file to output/uploads/{job_id}/{filename}
  2. Create job in job_store (status=pending)
  3. Submit _run_pipeline_sync to ThreadPoolExecutor
  4. Return {job_id, status: "pending"}

_run_pipeline_sync:
  1. Update job status=processing
  2. Call pipeline.run() with JobLogger capturing per-job logs
  3. On success: update job status=done, store artifact paths
  4. On failure: update job status=failed, store error message
```

### 20.3 Phase 2 Endpoints

**File:** `api/execution_routes.py` (prefix: `/api/v2`)

| Method | Path | Role | Description |
|--------|------|------|-------------|
| POST | `/executions` | executor | Start execution from CanonicalTestModel JSON |
| GET | `/executions` | reader | List executions (limit param) |
| GET | `/executions/{id}` | reader | Get full execution state |
| POST | `/executions/{id}/pause` | executor | Pause execution |
| POST | `/executions/{id}/resume` | executor | Resume paused execution |
| POST | `/executions/{id}/abort` | executor | Abort execution immediately |
| POST | `/executions/{id}/rollback` | executor | Trigger manual rollback |
| GET | `/executions/{id}/report` | reader | JSON report |
| GET | `/executions/{id}/report/html` | reader | HTML report |
| GET | `/executions/{id}/timeline` | reader | Sorted transition history |
| POST | `/approvals/{id}` | approver | Submit approval decision |
| POST | `/kill` | admin | Engage kill switch |
| DELETE | `/kill` | admin | Clear kill switch |
| GET | `/kill` | reader | Kill switch status |
| GET | `/metrics` | reader | Prometheus-format metrics |

**POST /executions request body:**
```json
{
  "canonical_model": { ... },
  "dry_run": false,
  "device_overrides": {}
}
```

Returns immediately with `{execution_id, status: "pending"}`. The execution runs in a background daemon thread.

**Background thread responsibilities:**
1. Run `ExecutionAgent.run(execution_id)`
2. On completion (success or failure): call `ExecutionReportBuilder.save(execution_id)`
3. If `canonical_model.change_ticket` is set: call ITSM client methods

**Prometheus metrics format:**
```
# HELP mop_executions_total Total executions by status
# TYPE mop_executions_total counter
mop_executions_total{status="passed"} 42
mop_executions_total{status="failed"} 3
# HELP mop_active_executions Currently running executions
# TYPE mop_active_executions gauge
mop_active_executions 1
```

### 20.4 Job Store

**File:** `api/job_store.py`

File-backed at `output/jobs/{job_id}.json`. One `threading.Lock` for all operations (no per-job locks needed since job creation is serialized by the executor).

Job fields: `job_id`, `status`, `filename`, `title`, `model`, `skip_toon`, `skip_guardrails`, `created_at`, `updated_at`, `progress_message`, `result` (dict), `error` (str), `output_dir`.

---

## 21. Configuration Files

### 21.1 execution_defaults.yaml

**File:** `configs/execution_defaults.yaml`

```yaml
defaults:
  retry_count: 3
  retry_delay_s: 10.0
  timeout_s: 30.0
  on_failure: abort      # abort | continue | rollback_all | rollback_group

limits:
  max_concurrent_devices: 5
  max_concurrent_steps_per_device: 1
  max_steps_per_execution: 500
  queue_starvation_timeout_s: 300

connection:
  idle_timeout_s: 600
  connect_timeout_s: 30

state:
  ttl_hours: 168

blast_radius:
  # Steps are auto-assigned blast radius based on command characteristics
  # Override per-step in CanonicalTestModel if needed
  thresholds:
    low: []           # Default
    medium: ["interface", "policy", "route-policy"]
    high: ["bgp", "ospf", "isis", "mpls"]
    critical: ["write erase", "format", "reload"]

blocked_commands:
  # These commands require elevated approval or are blocked outright
  - "write erase"
  - "format"
  - "crypto key zeroize"
  - "delete nvram:"
```

### 21.2 device_inventory.yaml

**File:** `configs/device_inventory.yaml`

```yaml
devices:
  PE-CORE-01:
    ip: 10.0.0.1
    platform: cisco_xr
    driver: netmiko
    port: 22
    max_connections: 2
    oob_host: oob-pe-core-01.mgmt.example.com

  PE-CORE-02:
    ip: 10.0.0.2
    platform: juniper_junos
    driver: netmiko
    port: 22
    max_connections: 2

  LEAF-SW-01:
    ip: 10.0.1.1
    platform: arista_eos
    driver: netmiko
    port: 22
    max_connections: 1
```

### 21.3 rbac.yaml

**File:** `configs/rbac.yaml`

```yaml
# Uncomment and add actual keys for production use
api_keys:
  # prod-executor-key: executor
  # prod-reader-key: reader
  # prod-approver-key: approver
  # prod-admin-key: admin

approval_quorum: 1
```

Empty file = dev mode (all requests are admin).

### 21.4 notifications.yaml

**File:** `configs/notifications.yaml`

```yaml
slack:
  enabled: false
  events:
    - execution_started
    - execution_passed
    - execution_failed
    - rollback_started
    - rollback_failed
    - kill_switch_engaged
    - approval_required

email:
  enabled: false
  events:
    - execution_failed
    - rollback_failed
    - kill_switch_engaged

pagerduty:
  enabled: false
  events:
    - execution_failed
    - rollback_failed
```

---

## 22. Test Suite

**Directory:** `tests/unit_tests/`

Total: **175 tests** across three files.

### 22.1 test_pipeline.py (121 tests)

**File:** `tests/unit_tests/test_pipeline.py`

Covers all Phase 1 pipeline components:

| Test Group | Count | What It Tests |
|-----------|-------|---------------|
| Document loader | ~12 | Magic byte detection, format routing, DOCX tracked-change stripping |
| PDF parser | ~10 | pdfplumber extraction, heading detection, OCR trigger threshold |
| Grammar engine | ~18 | Vendor patterns, protocol detection, prompt stripping, normalization |
| TOON builder | ~15 | Structure routing, node construction, prose scoring, compression ratio |
| TOON renderer | ~8 | Node format, pipe separators, rollback flag |
| Context chunker | ~10 | Bin-packing, oversized section splitting, chunk merge renumbering |
| LLM runner | ~12 | Retry logic, JSON extraction strategies, refusal detection, chunk merge |
| Mock LLM runner | ~10 | Section-aware dedup, _guess_section heuristics, _guess_expected |
| Guardrails | ~10 | All 6 check types, auto-correct, coverage threshold |
| Schema validator | ~8 | Round-trip validation, business rules |
| Generators | ~8 | Zephyr CSV columns, Robot Framework structure, CLI rules JSON |

Key test patterns:
```python
def test_magic_byte_pdf_detection():
    """Confirm %PDF header routes to pdf_parser even with .docx extension."""

def test_section_aware_dedup_preserves_precheck_and_verify():
    """Same command in Pre-checks and Verification must produce 2 steps."""

def test_toon_compression_ratio():
    """TOON output should be ≤20% of input for structured documents."""

def test_retry_json_parse_fail_sends_correction_message():
    """JSON_CORRECTION_MESSAGE must appear in conversation on retry."""

def test_guardrail_rollback_consistency_auto_correct():
    """Steps with 'rollback' in description but is_rollback=False must be fixed."""
```

### 22.2 test_agents.py (33 tests)

**File:** `tests/unit_tests/test_agents.py`

Covers Phase 2 agents with MockDriver:

| Test Group | Count | What It Tests |
|-----------|-------|---------------|
| DAG engine | ~8 | Wave generation, cycle detection, critical path, ready_steps |
| Planner agent | ~6 | Wave plan, approval check, blocked commands, dry run summary |
| Execution agent | ~10 | Wave loop, kill switch check, approval gate polling, retry flow |
| Validation agent | ~5 | passive match, active rules, negate mode, error pattern |
| Recovery agent | ~4 | RETRY→ESCALATE→ROLLBACK decision chain, decision log write |

Key test patterns:
```python
def test_dag_cycle_detection():
    """DAG with A→B→C→A must be detected as cyclic."""

def test_kill_switch_aborts_mid_wave():
    """Engaging kill switch during wave execution must abort remaining steps."""

def test_recovery_escalate_on_auth_failure():
    """'permission denied' in output must produce ESCALATE regardless of retry count."""
```

### 22.3 test_phase2_integration.py (21 tests)

**File:** `tests/unit_tests/test_phase2_integration.py`

End-to-end Phase 2 integration tests using MockDriver:

| Test | Description |
|------|-------------|
| `test_full_execution_dry_run` | PlannerAgent → ExecutionAgent on 5-step model, dry_run=True |
| `test_full_execution_with_rollback` | 3-step model, step 2 fails, ROLLBACK_ALL triggers rollback |
| `test_kill_switch_stops_execution` | Kill switch engaged after step 1, remaining steps aborted |
| `test_pause_resume_cycle` | Execution paused mid-wave, resumed, completes |
| `test_approval_gate_passes` | approval_required=True, approval submitted → execution continues |
| `test_approval_gate_denied` | approval_required=True, approval denied → execution aborted |
| `test_idempotency_skip` | Step with matching idempotency rule is skipped |
| `test_state_manager_persistence` | ExecutionState written/read from file correctly |
| `test_concurrent_waves` | Wave with 3 independent steps executes all in parallel |
| `test_rollback_reverse_order` | Rollback executes steps in reverse sequence order |

### 22.4 Running Tests

```bash
# All tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=. --cov-report=html

# Phase 1 only
pytest tests/unit_tests/test_pipeline.py -v

# Phase 2 only
pytest tests/unit_tests/test_agents.py tests/unit_tests/test_phase2_integration.py -v

# Specific test
pytest tests/unit_tests/test_pipeline.py::test_section_aware_dedup_preserves_precheck_and_verify -v
```

---

## 23. Deployment and Operations

### 23.1 Local Development

**Prerequisites:**
```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**Environment variables (minimum for Phase 1):**
```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

**Run Phase 1 CLI:**
```bash
python pipeline.py --input mop.pdf --output ./out
```

**Run API server:**
```bash
uvicorn api.main:app --reload --port 8000
```

**Offline / no API key:**
```bash
python pipeline.py --input mop.pdf --output ./out --mock-llm
```

### 23.2 Docker Deployment

**File:** `docker-compose.yml`

```yaml
services:
  mop-api:
    build: .
    ports:
      - "8000:8000"
    env_file: .env
    volumes:
      - ./output:/app/output
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
```

```bash
# Start
docker compose up -d

# View logs
docker compose logs -f mop-api

# Stop
docker compose down
```

**Required `.env` file:**
```bash
ANTHROPIC_API_KEY=sk-ant-...
MOP_API_KEY=your-admin-api-key   # Optional; if unset → dev mode (no auth)
MAX_WORKERS=3
LOG_LEVEL=INFO
LOG_DIR=logs
```

**Phase 2 environment variables (as needed):**
```bash
# Device credentials (per-device or default)
DEVICE_DEFAULT_USER=admin
DEVICE_DEFAULT_PASS=secret
DEVICE_DEFAULT_ENABLE=enable_secret

# Per-device override (hostname uppercased, dashes to underscores)
DEVICE_CREDS_PE_CORE_01_USER=admin
DEVICE_CREDS_PE_CORE_01_PASS=secret

# Vault (optional)
VAULT_ADDR=https://vault.example.com
VAULT_TOKEN=hvs.xxx

# Jump host (optional)
JUMP_HOST=bastion.example.com
JUMP_HOST_PE_CORE_01=bastion-prod.example.com  # Per-device override

# Credential file (optional)
CREDS_KEY=base64-encoded-32-byte-key

# Notifications (optional)
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
SMTP_HOST=smtp.example.com
SMTP_USER=noreply@example.com
SMTP_PASS=...
NOTIFY_EMAIL_TO=ops@example.com
PD_INTEGRATION_KEY=...

# ITSM (optional)
ITSM_URL=https://example.service-now.com
ITSM_USERNAME=api_user
ITSM_PASSWORD=...   # ServiceNow basic auth
ITSM_TOKEN=...      # Jira API token
```

### 23.3 API Usage Examples

**Phase 1 — Process a document:**
```bash
curl -X POST http://localhost:8000/api/v1/process \
  -H "X-Api-Key: your-admin-key" \
  -F "file=@bgp_mop.pdf" \
  -F "title=BGP Route Policy Change"
# Returns: {"job_id": "abc123", "status": "pending"}

# Poll status
curl http://localhost:8000/api/v1/status/abc123

# Download Robot file
curl -o bgp_mop.robot http://localhost:8000/api/v1/download/abc123/robot
```

**Phase 2 — Execute a canonical model:**
```bash
# Start execution
curl -X POST http://localhost:8000/api/v2/executions \
  -H "X-Api-Key: your-executor-key" \
  -H "Content-Type: application/json" \
  -d '{"canonical_model": {...}, "dry_run": false}'
# Returns: {"execution_id": "xyz789", "status": "pending"}

# Poll status
curl -H "X-Api-Key: your-reader-key" \
  http://localhost:8000/api/v2/executions/xyz789

# Pause
curl -X POST http://localhost:8000/api/v2/executions/xyz789/pause \
  -H "X-Api-Key: your-executor-key" \
  -d '{"reason": "Maintenance window closing"}'

# Submit approval
curl -X POST http://localhost:8000/api/v2/approvals/xyz789 \
  -H "X-Api-Key: your-approver-key" \
  -d '{"approver_id": "jsmith", "decision": "approved", "comment": "Reviewed and approved"}'

# Engage kill switch (admin only)
curl -X POST http://localhost:8000/api/v2/kill \
  -H "X-Api-Key: your-admin-key"

# Get HTML report
curl -H "X-Api-Key: your-reader-key" \
  http://localhost:8000/api/v2/executions/xyz789/report/html > report.html
```

### 23.4 Monitoring

**Health check:**
```bash
curl http://localhost:8000/health
# {"status": "ok", "version": "1.0.0", "kill_switch": false}
```

**Prometheus metrics:**
```bash
curl -H "X-Api-Key: your-reader-key" http://localhost:8000/api/v2/metrics
```

**Log files** (written to `logs/` directory):
- `logs/mop_platform.log` — main rotating log (10MB × 5 backups)
- `logs/mop_platform_errors.log` — errors only
- `output/sessions/{hostname}_{timestamp}.log` — per-device SSH session logs
- `output/decision.log` — all recovery decisions (append-only, one JSON per line)

### 23.5 Known Limitations and Pending Work

Items not yet implemented (tracked in `project_pending_work.md`):

1. **OCR fallback** is available but requires manual installation of `pdf2image`, `pytesseract`, and the Tesseract binary. Not included in `requirements.txt` by default.

2. **Tracked-changes DOCX** — the lxml stripping is implemented but edge cases with complex revision markup may not strip cleanly.

3. **Additional vendor tests** — Huawei VRP, Ericsson, F5, PaloAlto, CheckPoint have grammar patterns but fewer test cases than Cisco/Juniper/Arista.

4. **Magic bytes detection for other formats** — only PDF and DOCX/ZIP are magic-byte detected. ODP, XLSX, and other ZIP-based formats not supported.

5. **WebSocket streaming** — execution status is currently polled via REST. Real-time streaming via WebSocket is not yet implemented.

6. **Multi-device orchestration** — the DAG engine supports multi-device steps via `step.devices` list, but the execution agent currently picks the first device. Load distribution across multiple devices for the same step is not implemented.

7. **Credential rotation** — the credential store reads at connection time but does not support mid-execution credential rotation (e.g., for Vault dynamic secrets).

8. **TOON for tables** — table rows are extracted as TOON nodes but complex multi-column tables with merged cells may produce incorrect node text.

### 23.6 Adding a New Vendor

To add support for a new network device vendor:

1. **Grammar engine** (`grammar_engine/protocol_patterns.yaml`): Add vendor-specific CLI command patterns with confidence scores.

2. **Device driver** (`device_layer/device_driver.py`): Add entry to `_VENDOR_TO_DEVICE_TYPE` mapping the vendor name to the correct Netmiko device type.

3. **Recovery agent** (`execution_engine/recovery_agent.py`): Add the vendor to `_build_rollback_commands()` with the appropriate negation prefix (e.g., `undo ` for Huawei, `no ` for Cisco-style).

4. **Mock LLM runner** (`ai_layer/mock_llm_runner.py`): The `_make_tags()` and `_make_description()` functions use `dc.vendor` — no changes needed unless the vendor needs special description formatting.

5. **Tests**: Add grammar pattern tests to `tests/unit_tests/test_pipeline.py` and execution tests to `tests/unit_tests/test_agents.py`.

---

*End of document. Total sections: 23. This document reflects all code implemented as of 2026-03-25.*
