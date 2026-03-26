Discover network topology from a live device or plain English description.

**Usage:** /discover-topology [arguments]
**Arguments:** $ARGUMENTS

## Steps

1. **Parse arguments** from: $ARGUMENTS
   - `--seed <ip>` → live LLDP/CDP discovery from that IP
   - `--describe "<text>"` → generate from plain English description
   - `--mock` → generate a sample topology for testing

2. **If --seed provided (live discovery):**
   - Run: `python standalone_tester/discover.py --seed <ip> --username <user> --depth 2`
   - Show the discovered devices and generated topology file path
   - Ask the user if they want to run tests immediately against the discovered topology

3. **If --describe provided:**
   - Run: `python standalone_tester/discover.py --describe "<text>" --name "lab_topology"`
   - Show the generated YAML
   - Suggest what tests to run based on the described topology

4. **If --mock:**
   - Run: `python standalone_tester/discover.py --mock`
   - Show the mock topology
   - Suggest: `python standalone_tester/run_tests.py --topology <path> --protocol all --test smoke --mock-ssh`

5. **Always:**
   - Read the generated topology file and summarise what was found
   - Suggest the next command to run tests against it
