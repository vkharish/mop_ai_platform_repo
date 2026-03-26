#!/usr/bin/env python3
"""
Protocol Test Runner — CLI entrypoint for the Python agent.

Usage:
  python standalone_tester/run_tests.py --topology hybrid/sample_mpls_lab.yaml --protocol bgp --test gating
  python standalone_tester/run_tests.py --topology hybrid/sample_mpls_lab.yaml --protocol all --test smoke
  python standalone_tester/run_tests.py --topology hybrid/sample_mpls_lab.yaml --protocol bgp --test gating --device PE1
  python standalone_tester/run_tests.py --topology hybrid/sample_mpls_lab.yaml --protocol isis --test certification --vendor cisco
  python standalone_tester/run_tests.py --topology hybrid/sample_mpls_lab.yaml --protocol bgp --test gating --mock-llm --mock-ssh
"""
import argparse
import json
import logging
import sys
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s — %(message)s")
logger = logging.getLogger("run_tests")


def main():
    parser = argparse.ArgumentParser(
        description="Standalone Protocol Test Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python standalone_tester/run_tests.py --topology hybrid/sample_mpls_lab.yaml --protocol bgp --test gating
  python standalone_tester/run_tests.py --topology hybrid/sample_mpls_lab.yaml --protocol all --test smoke --mock-llm --mock-ssh
  python standalone_tester/run_tests.py --topology hybrid/sample_mpls_lab.yaml --protocol isis --test certification --device PE1
        """
    )
    parser.add_argument("--topology", "-t", required=True,
                        help="Topology file path (relative to inventory/topologies/ or absolute)")
    parser.add_argument("--protocol", "-p", default="bgp",
                        help="Protocol to test: bgp|isis|mpls|interface|system|all")
    parser.add_argument("--test", default="gating",
                        help="Test type: smoke|gating|certification")
    parser.add_argument("--device", "-d", help="Specific device name(s), comma-separated")
    parser.add_argument("--vendor", help="Filter by vendor: cisco|juniper|nokia|arista|huawei")
    parser.add_argument("--role", help="Filter by role: pe-router|p-router|route-reflector")
    parser.add_argument("--mock-llm", action="store_true",
                        help="Use heuristic command translation (no LLM call, no API key needed)")
    parser.add_argument("--mock-ssh", action="store_true",
                        help="Use mock SSH driver (no real devices needed)")
    parser.add_argument("--output", "-o", help="Save JSON report to this file")
    parser.add_argument("--api-key", help="Anthropic API key (or set ANTHROPIC_API_KEY env var)")
    args = parser.parse_args()

    from standalone_tester.agent.protocol_test_agent import ProtocolTestAgent

    agent = ProtocolTestAgent(
        api_key=args.api_key,
        mock_ssh=args.mock_ssh,
        mock_llm=args.mock_llm,
    )

    suite = agent.run(
        topology_path=args.topology,
        protocol=args.protocol,
        test_type=args.test,
        device_filter=args.device,
        vendor_filter=args.vendor,
        role_filter=args.role,
    )

    # Print per-device reports
    for report in suite.device_reports:
        report.print_report()

    # Print suite summary
    suite.print_summary()

    # Save JSON report
    if args.output:
        import dataclasses
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(dataclasses.asdict(suite), f, indent=2)
        print(f"\nReport saved → {args.output}")

    sys.exit(0 if suite.overall_status == "PASS" else 1)


if __name__ == "__main__":
    main()
