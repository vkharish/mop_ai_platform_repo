Run a full lab certification test suite against a topology.

**Usage:** /run-certification [arguments]
**Arguments:** $ARGUMENTS

## What certification means
A full certification run tests ALL protocols on ALL devices at the `certification` level.
This is what lab engineers run before signing off a new software release or major config change.

## Steps

1. **Parse arguments** from: $ARGUMENTS
   Expected: `--topology <file> [--protocol <proto>] [--mock-ssh] [--mock-llm]`

2. **Pre-flight check** (always run first):
   Run: `python standalone_tester/run_tests.py --topology <topology> --protocol system --test smoke --mock-ssh`
   - If any device is UNREACHABLE → stop, report which devices are down
   - If CPU/memory critical → warn before proceeding

3. **Run certification tests** per protocol in this order:
   - system (gating first — confirms devices are stable)
   - interface (all links up before testing protocols)
   - isis or ospf (IGP must be healthy before BGP/MPLS)
   - mpls (label distribution needs IGP)
   - bgp (overlay needs underlay)

   For each: `python standalone_tester/run_tests.py --topology <topology> --protocol <proto> --test certification [--mock-ssh] [--mock-llm]`

4. **For any FAILED tests:**
   - Read the failure details from the output
   - Reason about the root cause
   - Check if it's a critical failure (stop certification) or advisory (continue with warning)

5. **Generate certification verdict:**
   - CERTIFIED: all critical tests passed
   - CONDITIONAL: non-critical failures only, list what to monitor
   - NOT CERTIFIED: critical failures, list blocking issues

6. **Save report:**
   Run: `python standalone_tester/run_tests.py --topology <topology> --protocol all --test certification --output reports/cert_<timestamp>.json`
