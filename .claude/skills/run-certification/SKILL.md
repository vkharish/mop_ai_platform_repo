---
name: run-certification
description: Run a full lab certification test suite against a topology. Tests all protocols in dependency order (system → interface → IGP → MPLS → BGP). Produces a CERTIFIED / CONDITIONAL / NOT CERTIFIED verdict.
disable-model-invocation: true
allowed-tools: Read, Bash
argument-hint: "--topology <file> [--protocol <proto>] [--mock-ssh] [--mock-llm]"
---

Run a full lab certification suite.

**Arguments:** $ARGUMENTS

## What certification means

A full cert run tests ALL protocols in dependency order before signing off a
software release or major config change. I run each protocol, assess failures,
and give a final verdict.

## Steps

1. **Parse arguments** — topology file is required. Defaults: protocol=all, test=certification.

2. **Pre-flight (system smoke test)**
   ```bash
   python standalone_tester/run_tests.py --topology <topology> --protocol system --test smoke [--mock-ssh] [--mock-llm]
   ```
   - Any UNREACHABLE device → STOP, report which and why
   - CPU/memory critical → WARN but continue

3. **Run in dependency order** (each must pass before the next starts):

   **Step A — Interfaces**
   ```bash
   python standalone_tester/run_tests.py --topology <topology> --protocol interface --test certification [--mock-ssh] [--mock-llm]
   ```

   **Step B — IGP (ISIS)**
   ```bash
   python standalone_tester/run_tests.py --topology <topology> --protocol isis --test certification [--mock-ssh] [--mock-llm]
   ```

   **Step C — MPLS**
   ```bash
   python standalone_tester/run_tests.py --topology <topology> --protocol mpls --test certification [--mock-ssh] [--mock-llm]
   ```

   **Step D — BGP**
   ```bash
   python standalone_tester/run_tests.py --topology <topology> --protocol bgp --test certification [--mock-ssh] [--mock-llm]
   ```

4. **Assess each result:**
   - Critical failure → mark as BLOCKING, note which step failed
   - Non-critical failure → mark as ADVISORY, continue
   - If a step fails critically → skip dependent steps (e.g. if IGP fails, skip MPLS/BGP)

5. **Final verdict:**
   - ✅ **CERTIFIED** — all critical tests passed across all protocols
   - ⚠️ **CONDITIONAL** — non-critical failures only; list what to monitor post-change
   - ❌ **NOT CERTIFIED** — critical failures; list blocking issues that must be resolved

6. **Save report:**
   ```bash
   python standalone_tester/run_tests.py --topology <topology> --protocol all --test certification --output standalone_tester/reports/cert_$(date +%Y%m%d_%H%M%S).json [--mock-ssh] [--mock-llm]
   ```
   Report the saved path so the engineer can use it as a baseline later.
