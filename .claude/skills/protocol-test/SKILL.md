---
name: protocol-test
description: Run a protocol health test on network devices using the lab inventory. Use for interactive investigation when you need to understand WHY something failed, not just THAT it failed.
disable-model-invocation: true
allowed-tools: Read, Bash, Glob
argument-hint: "--device <name> --protocol <bgp|isis|mpls|interface|system|all> --test <smoke|gating|certification> [--topology <file>] [--mock-ssh]"
---

Run a protocol health test on network devices.

**Arguments:** $ARGUMENTS

## What I do (full reasoning mode)

I am the INTERACTIVE version of the protocol tester. Unlike the Python agent that
just pattern-matches, I read the actual device output and REASON about what I find.
I investigate failures, run additional diagnostics, and explain WHY something failed.

## Steps

1. **Parse arguments** from: $ARGUMENTS
   - `--device <name>` — device name from inventory (e.g. PE1, RR1)
   - `--protocol <proto>` — bgp | isis | mpls | interface | system | all
   - `--test <type>` — smoke | gating | certification (default: gating)
   - `--topology <file>` — path relative to standalone_tester/inventory/topologies/ (default: hybrid/sample_mpls_lab.yaml)
   - `--mock-ssh` — simulate SSH output (no real device needed)

2. **Load the inventory**
   Read the topology file:
   `standalone_tester/inventory/topologies/<topology>`

   For the requested device, resolve the vendor ref:
   `standalone_tester/inventory/vendors/<vendor>/<os>/<model>.yaml`
   and its defaults:
   `standalone_tester/inventory/vendors/<vendor>/<os>/_defaults.yaml`

3. **Load the test catalog**
   Read: `standalone_tester/test_catalog/catalog.yaml`
   Get the tests for: protocol + test_type

4. **For each test intent:**

   If `--mock-ssh` was given:
   - Use your knowledge of the vendor/OS to describe what healthy output would look like
   - Simulate a realistic response and assess it

   If no `--mock-ssh` (real device):
   - Determine the exact CLI command for this vendor/OS
   - Run: `ssh <username>@<host> "<command>"` via Bash
   - Read the output carefully — does it indicate healthy state?
   - If something looks WRONG: run 1-2 additional diagnostic commands autonomously
   - Don't just say FAIL — explain what you found and why it matters

5. **Report findings:**
   - Per-test: ✅ PASS / ❌ FAIL with evidence from the actual output
   - For failures: root cause assessment + recommended next step
   - Overall: PASS / FAIL / WARN with confidence level

## Key differentiator from Python agent

```bash
# Python agent — fast, cheap, automated (no reasoning)
python standalone_tester/run_tests.py --topology hybrid/sample_mpls_lab.yaml --protocol bgp --test gating --mock-ssh --mock-llm

# This skill — interactive, full reasoning, investigates failures
/protocol-test --device PE1 --protocol bgp --test gating --mock-ssh
```
