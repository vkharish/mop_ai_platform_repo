---
name: discover-topology
description: Discover network lab topology from a live seed device (LLDP/CDP) or from a plain English description. Generates a structured inventory YAML file ready for protocol testing.
disable-model-invocation: true
allowed-tools: Read, Bash, Write
argument-hint: "--seed <ip> | --describe \"<text>\" | --mock [--name <topology-name>]"
---

Discover network topology and generate an inventory file.

**Arguments:** $ARGUMENTS

## Three modes

### Mode 1: `--seed <ip>` — Live LLDP/CDP discovery
```
/discover-topology --seed 192.168.1.1
```
Run the Python discovery agent:
```bash
python standalone_tester/discover.py --seed <ip> --username <user> --depth 2
```
Then read the generated file, summarise what was discovered, and suggest next tests.

### Mode 2: `--describe "<text>"` — Generate from plain English
```
/discover-topology --describe "2 Cisco ASR9006 PEs running IOS-XR 7.5.1, 1 Nokia 7750 P router on SR OS 23.10, connected via IS-IS and MPLS"
```
Run:
```bash
python standalone_tester/discover.py --describe "<text>" --name "<name>"
```
Read the generated YAML, display it, and suggest:
```
/protocol-test --topology generated/<filename> --protocol all --test smoke
```

### Mode 3: `--mock` — Sample topology for testing (no devices needed)
```
/discover-topology --mock
```
Run:
```bash
python standalone_tester/discover.py --mock
```
Show the generated topology and suggest:
```
/protocol-test --topology <path> --protocol bgp --test gating --mock-ssh
```

## Always

- Read the generated topology file after creation
- Show the device list: name, vendor, OS, version, role, host IP
- Suggest the exact next command to run tests against it
- If `--seed` mode and devices were not reachable, explain why and suggest fixes
