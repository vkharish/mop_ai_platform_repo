---
name: compare-baseline
description: Compare current network state against a saved baseline report. Used after a MOP or change to verify what changed, whether changes were expected, and if any regressions were introduced.
disable-model-invocation: true
allowed-tools: Read, Bash
argument-hint: "--topology <file> --baseline <json-file> [--protocol <proto>] [--mock-ssh] [--mock-llm]"
---

Compare current network state against a saved baseline.

**Arguments:** $ARGUMENTS

## Purpose

After executing a MOP or making a change, compare the current state against a
pre-change baseline snapshot. Find regressions, confirm expected improvements,
and give a change verdict.

## Steps

1. **Parse arguments**
   - `--topology <file>` — required
   - `--baseline <json>` — path to saved baseline report JSON
   - `--protocol` — default: bgp (match whatever protocol the baseline covers)

2. **Capture current state**
   ```bash
   python standalone_tester/run_tests.py \
     --topology <topology> \
     --protocol <protocol> \
     --test gating \
     --output standalone_tester/reports/current_$(date +%Y%m%d_%H%M%S).json \
     [--mock-ssh] [--mock-llm]
   ```

3. **Load both reports**
   - Read the baseline JSON file specified in `--baseline`
   - Read the current JSON file just generated

4. **Compare per device, per test:**

   | Before → After | Meaning |
   |----------------|---------|
   | PASS → PASS | Stable — no change |
   | PASS → FAIL | **REGRESSION** — change broke something |
   | FAIL → PASS | Improvement — change fixed a pre-existing issue |
   | FAIL → FAIL | Pre-existing issue — not caused by this change |

5. **Report:**
   - List all **regressions** (PASS→FAIL) — these need immediate attention
   - List all **improvements** (FAIL→PASS) — confirm these were intended
   - List stable tests (PASS→PASS count)
   - Assess each regression: is it critical? Does it need rollback?

6. **Final change verdict:**
   - ✅ **SAFE** — no regressions, all changes as expected
   - ⚠️ **MIXED** — some regressions but non-critical; list mitigations
   - ❌ **CAUSED REGRESSIONS** — critical failures introduced; recommend rollback
