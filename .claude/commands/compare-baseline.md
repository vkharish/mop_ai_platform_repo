Compare current network state against a saved baseline.

**Usage:** /compare-baseline [arguments]
**Arguments:** $ARGUMENTS

## Purpose
After a MOP is executed or a change is made, compare the current state
against a pre-change baseline to verify: what changed, was it expected,
and is the network healthy?

## Steps

1. **Parse arguments** from: $ARGUMENTS
   Expected: `--topology <file> --baseline <json_file> [--protocol <proto>]`

2. **Capture current state:**
   Run: `python standalone_tester/run_tests.py --topology <topology> --protocol <proto> --test gating --output reports/current_<timestamp>.json`

3. **Load both reports:**
   - Read the baseline JSON file
   - Read the current JSON file just generated

4. **Compare per device, per test:**
   - PASS→PASS: no change (expected)
   - PASS→FAIL: regression — something broke after the change
   - FAIL→PASS: improvement — the change fixed a pre-existing issue
   - FAIL→FAIL: pre-existing issue, not caused by the change

5. **Report:**
   - List all regressions (PASS→FAIL) — these need immediate attention
   - List all improvements (FAIL→PASS) — confirm these were intended
   - Confirm stable tests (PASS→PASS)
   - Give overall verdict: change was SAFE / CAUSED REGRESSIONS / MIXED

6. **If regressions found:**
   Use your knowledge to suggest likely causes and whether rollback is needed.
