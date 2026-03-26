Run a protocol health test on network devices using the lab inventory.

**Usage:** /protocol-test [arguments]
**Arguments:** $ARGUMENTS

## What I do (Claude Code skill — full reasoning mode)

I am the INTERACTIVE version of the protocol tester. Unlike the Python agent that
just pattern-matches, I SSH to devices, read the actual output, and REASON about
what I find. I investigate failures, run additional diagnostics, and explain WHY
something failed — not just THAT it failed.

## Steps

1. **Parse arguments** from: $ARGUMENTS
   Expected format: `--device <name> --protocol <proto> --test <type> [--topology <file>]`
   Defaults: protocol=bgp, test=gating, topology=hybrid/sample_mpls_lab.yaml

2. **Load the inventory**
   - Read the topology file from `standalone_tester/inventory/topologies/`
   - Read the vendor template from `standalone_tester/inventory/vendors/`
   - Identify: vendor, OS, version, host IP for the requested device

3. **Load the test catalog**
   - Read `standalone_tester/test_catalog/catalog.yaml`
   - Get the test intents for the requested protocol + test type

4. **For each test intent:**
   - Based on the vendor/OS, determine the correct CLI command (use your knowledge)
   - Run it: `ssh <username>@<host> "<command>"` using the Bash tool
   - Read the output carefully
   - Ask yourself: does this indicate healthy state?
   - If something looks WRONG: run additional diagnostic commands to investigate
   - Don't just say FAIL — explain what you found and why it's a problem

5. **Report findings:**
   - Per-test: PASS / FAIL with specific evidence from the output
   - For failures: root cause assessment and recommended next step
   - Overall: summary with confidence level

## Differentiator from Python agent
The Python agent runs `/usr/bin/python run_tests.py` — fast, cheap, pattern-match only.
I (Claude Code) reason about output, adapt my test plan, investigate anomalies,
and give you actionable findings. Use me when you need to UNDERSTAND what's happening,
use the Python agent when you need automated regression at scale.
