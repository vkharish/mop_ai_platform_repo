#!/usr/bin/env python3
"""
Topology Discovery CLI

Two modes:
  1. Live discovery via LLDP/CDP from a seed device
  2. Generate from plain English description via LLM

Usage:
  python standalone_tester/discover.py --seed 192.168.1.1 --username admin --depth 2
  python standalone_tester/discover.py --describe "2 Cisco ASR9006 PEs and 1 Nokia 7750 P router"
  python standalone_tester/discover.py --mock   (generates mock topology for testing)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    parser = argparse.ArgumentParser(description="Topology Discovery")
    parser.add_argument("--seed", help="Seed device IP for live LLDP/CDP discovery")
    parser.add_argument("--username", default="admin", help="SSH username")
    parser.add_argument("--password", default="", help="SSH password (or set LAB_PASS env var)")
    parser.add_argument("--depth", type=int, default=2,
                        help="Discovery depth — how many hops from seed (default: 2)")
    parser.add_argument("--describe", metavar="TEXT",
                        help="Describe topology in plain English — LLM generates inventory")
    parser.add_argument("--name", help="Name for the generated topology")
    parser.add_argument("--mock", action="store_true",
                        help="Generate a mock topology for testing (no SSH, no API)")
    parser.add_argument("--api-key", help="Anthropic API key for --describe mode")
    args = parser.parse_args()

    from standalone_tester.discovery.topology_discovery import TopologyDiscoveryAgent
    import os

    if args.mock:
        agent = TopologyDiscoveryAgent(mock=True)
        path = agent.discover_from_description("", args.name or "mock_lab")
        print(f"Mock topology generated: {path}")
        return

    agent = TopologyDiscoveryAgent(mock=False)

    if args.seed:
        password = args.password or os.environ.get("LAB_PASS", "")
        path = agent.discover_live(
            seed_host=args.seed,
            username=args.username,
            password=password,
            depth=args.depth,
            topology_name=args.name or f"discovered_{args.seed}",
        )
        print(f"\nTopology file: {path}")
        print(f"Run tests: python standalone_tester/run_tests.py --topology {path} --protocol bgp --test gating")

    elif args.describe:
        path = agent.discover_from_description(
            description=args.describe,
            topology_name=args.name or "described_topology",
            api_key=args.api_key,
        )
        print(f"\nTopology file: {path}")
        print(f"Run tests: python standalone_tester/run_tests.py --topology {path} --protocol all --test smoke")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
