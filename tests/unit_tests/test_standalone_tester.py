"""
Tests for the standalone protocol tester.
All tests use mock SSH and mock LLM — no real devices or API keys needed.
"""
from __future__ import annotations
import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

TESTER_ROOT = Path(__file__).parent.parent.parent / "standalone_tester"


# ---------------------------------------------------------------------------
# VersionDetector
# ---------------------------------------------------------------------------
class TestVersionDetector:

    def test_detect_cisco_ios_xr(self):
        from standalone_tester.discovery.version_detector import VersionDetector
        output = """
        Cisco IOS XR Software, Version 7.5.1
        Copyright (c) 2013-2022 by Cisco Systems
        cisco ASR9006 (Intel 686 F6M14S4) processor
        PE1 uptime is 2 days
        """
        d = VersionDetector.detect(output)
        assert d.vendor == "cisco"
        assert d.os == "ios-xr"
        assert d.version == "7.5.1"

    def test_detect_juniper_junos(self):
        from standalone_tester.discovery.version_detector import VersionDetector
        output = """
        Hostname: RR1
        Model: mx960
        JUNOS 22.4R1.10 #0
        """
        d = VersionDetector.detect(output)
        assert d.vendor == "juniper"
        assert d.os == "junos"
        assert d.hostname == "RR1"

    def test_detect_nokia_sros(self):
        from standalone_tester.discovery.version_detector import VersionDetector
        output = """
        TiMOS-B-23.10.R1 both/x86_64
        Nokia 7750 SR-12
        System Name: P1
        """
        d = VersionDetector.detect(output)
        assert d.vendor == "nokia"
        assert d.os == "sros"

    def test_detect_arista_eos(self):
        from standalone_tester.discovery.version_detector import VersionDetector
        output = "Arista DCS-7280CR2-60 \nEOS version: 4.28.1F\nHostname: SW1"
        d = VersionDetector.detect(output)
        assert d.vendor == "arista"
        assert d.os == "eos"

    def test_detect_huawei_vrp(self):
        from standalone_tester.discovery.version_detector import VersionDetector
        output = "Huawei Versatile Routing Platform Software\nVRP (R) software, Version 8.180\n<PE1>"
        d = VersionDetector.detect(output)
        assert d.vendor == "huawei"
        assert d.os == "vrp"

    def test_detect_unknown(self):
        from standalone_tester.discovery.version_detector import VersionDetector
        d = VersionDetector.detect("random unrecognised output")
        assert d.vendor == "unknown"


# ---------------------------------------------------------------------------
# InventoryManager
# ---------------------------------------------------------------------------
class TestInventoryManager:

    def test_load_sample_topology(self):
        from standalone_tester.agent.inventory_manager import InventoryManager
        mgr = InventoryManager()
        devices = mgr.load_topology("hybrid/sample_mpls_lab.yaml")
        assert "PE1" in devices
        assert "P1" in devices
        assert "RR1" in devices

    def test_device_vendor_resolved(self):
        from standalone_tester.agent.inventory_manager import InventoryManager
        mgr = InventoryManager()
        devices = mgr.load_topology("hybrid/sample_mpls_lab.yaml")
        assert devices["PE1"].vendor == "cisco"
        assert devices["PE1"].os == "ios-xr"
        assert devices["RR1"].vendor == "juniper"
        assert devices["P1"].vendor == "nokia"

    def test_filter_by_vendor(self):
        from standalone_tester.agent.inventory_manager import InventoryManager
        mgr = InventoryManager()
        devices = mgr.load_topology("hybrid/sample_mpls_lab.yaml")
        cisco_only = mgr.filter_by_vendor(devices, "cisco")
        assert all(d.vendor == "cisco" for d in cisco_only.values())

    def test_filter_by_role(self):
        from standalone_tester.agent.inventory_manager import InventoryManager
        mgr = InventoryManager()
        devices = mgr.load_topology("hybrid/sample_mpls_lab.yaml")
        pes = mgr.filter_by_role(devices, "pe-router")
        assert all(d.role == "pe-router" for d in pes.values())

    def test_device_supports_protocol(self):
        from standalone_tester.agent.inventory_manager import InventoryManager
        mgr = InventoryManager()
        devices = mgr.load_topology("hybrid/sample_mpls_lab.yaml")
        assert devices["PE1"].supports_protocol("bgp")
        assert devices["PE1"].supports_protocol("isis")

    def test_topology_not_found_raises(self):
        from standalone_tester.agent.inventory_manager import InventoryManager
        mgr = InventoryManager()
        with pytest.raises(FileNotFoundError):
            mgr.load_topology("nonexistent/topology.yaml")


# ---------------------------------------------------------------------------
# CatalogManager
# ---------------------------------------------------------------------------
class TestCatalogManager:

    def test_load_catalog(self):
        from standalone_tester.agent.catalog_manager import CatalogManager
        mgr = CatalogManager()
        tests = mgr.get_tests("bgp", "gating")
        assert len(tests) > 0

    def test_bgp_smoke_tests(self):
        from standalone_tester.agent.catalog_manager import CatalogManager
        mgr = CatalogManager()
        tests = mgr.get_tests("bgp", "smoke")
        assert any(t.id == "bgp_neighbors_up" for t in tests)

    def test_isis_certification_has_convergence(self):
        from standalone_tester.agent.catalog_manager import CatalogManager
        mgr = CatalogManager()
        tests = mgr.get_tests("isis", "certification")
        sla_tests = [t for t in tests if t.sla_seconds > 0]
        assert len(sla_tests) > 0

    def test_empty_for_unknown_protocol(self):
        from standalone_tester.agent.catalog_manager import CatalogManager
        mgr = CatalogManager()
        tests = mgr.get_tests("unknown_protocol", "gating")
        assert tests == []

    def test_supported_protocols(self):
        from standalone_tester.agent.catalog_manager import CatalogManager
        mgr = CatalogManager()
        protocols = mgr.supported_protocols()
        for proto in ("bgp", "isis", "mpls", "interface", "system"):
            assert proto in protocols


# ---------------------------------------------------------------------------
# CommandTranslator (mock mode — no API key)
# ---------------------------------------------------------------------------
class TestCommandTranslator:

    def test_bgp_cisco(self):
        from standalone_tester.agent.command_translator import CommandTranslator
        t = CommandTranslator(mock=True)
        cmd = t.translate("All BGP neighbors Established", "bgp_neighbors_up", "cisco", "ios-xr", "7.5.1")
        assert "bgp" in cmd.command.lower()
        assert cmd.success_pattern

    def test_bgp_juniper(self):
        from standalone_tester.agent.command_translator import CommandTranslator
        t = CommandTranslator(mock=True)
        cmd = t.translate("All BGP neighbors Established", "bgp_neighbors_up", "juniper", "junos", "22.4")
        assert "bgp" in cmd.command.lower()

    def test_bgp_nokia(self):
        from standalone_tester.agent.command_translator import CommandTranslator
        t = CommandTranslator(mock=True)
        cmd = t.translate("All BGP neighbors Established", "bgp_neighbors_up", "nokia", "sros", "23.10")
        assert "bgp" in cmd.command.lower()

    def test_isis_command(self):
        from standalone_tester.agent.command_translator import CommandTranslator
        t = CommandTranslator(mock=True)
        cmd = t.translate("IS-IS adjacencies UP", "isis_adjacencies_up", "cisco", "ios-xr", "7.5.1")
        assert "isis" in cmd.command.lower()

    def test_cache_hit(self, tmp_path):
        from standalone_tester.agent.command_translator import CommandTranslator
        with patch("standalone_tester.agent.command_translator.CACHE_PATH", tmp_path / "cache.json"):
            t = CommandTranslator(mock=True)
            # First call
            cmd1 = t.translate("CPU check", "cpu_normal", "cisco", "ios-xr", "7.5.1")
            assert not cmd1.from_cache
            # Second call — should hit cache
            t2 = CommandTranslator(mock=True)
            cmd2 = t2.translate("CPU check", "cpu_normal", "cisco", "ios-xr", "7.5.1")
            assert cmd2.from_cache


# ---------------------------------------------------------------------------
# ProtocolTestAgent (mock SSH + mock LLM)
# ---------------------------------------------------------------------------
class TestProtocolTestAgent:

    def test_run_bgp_gating_mock(self):
        from standalone_tester.agent.protocol_test_agent import ProtocolTestAgent
        agent = ProtocolTestAgent(mock_ssh=True, mock_llm=True)
        suite = agent.run(
            topology_path="hybrid/sample_mpls_lab.yaml",
            protocol="bgp",
            test_type="gating",
        )
        assert suite.topology_name
        assert len(suite.device_reports) > 0

    def test_device_filter(self):
        from standalone_tester.agent.protocol_test_agent import ProtocolTestAgent
        agent = ProtocolTestAgent(mock_ssh=True, mock_llm=True)
        suite = agent.run(
            topology_path="hybrid/sample_mpls_lab.yaml",
            protocol="bgp",
            test_type="smoke",
            device_filter="PE1",
        )
        assert all(r.device_name == "PE1" for r in suite.device_reports)

    def test_vendor_filter(self):
        from standalone_tester.agent.protocol_test_agent import ProtocolTestAgent
        agent = ProtocolTestAgent(mock_ssh=True, mock_llm=True)
        suite = agent.run(
            topology_path="hybrid/sample_mpls_lab.yaml",
            protocol="bgp",
            test_type="smoke",
            vendor_filter="cisco",
        )
        assert all(r.vendor == "cisco" for r in suite.device_reports)

    def test_results_have_status(self):
        from standalone_tester.agent.protocol_test_agent import ProtocolTestAgent
        agent = ProtocolTestAgent(mock_ssh=True, mock_llm=True)
        suite = agent.run(
            topology_path="hybrid/sample_mpls_lab.yaml",
            protocol="bgp",
            test_type="smoke",
            device_filter="PE1",
        )
        for report in suite.device_reports:
            for result in report.results:
                assert result.status in ("PASS", "FAIL", "SKIP", "ERROR")


# ---------------------------------------------------------------------------
# ResultModel
# ---------------------------------------------------------------------------
class TestResultModel:

    def _make_report(self, pass_count=2, fail_count=1):
        from standalone_tester.agent.result_model import DeviceTestReport, TestResult
        report = DeviceTestReport(
            device_name="PE1", vendor="cisco", os="ios-xr",
            version="7.5.1", host="10.0.0.1",
            protocol="bgp", test_type="gating",
        )
        for i in range(pass_count):
            report.results.append(TestResult(
                test_id=f"pass_{i}", intent="test", severity="high", status="PASS"
            ))
        for i in range(fail_count):
            report.results.append(TestResult(
                test_id=f"fail_{i}", intent="test", severity="critical", status="FAIL"
            ))
        return report

    def test_pass_count(self):
        report = self._make_report(pass_count=3, fail_count=1)
        assert report.passed == 3

    def test_fail_count(self):
        report = self._make_report(pass_count=2, fail_count=2)
        assert report.failed == 2

    def test_overall_fail_on_critical(self):
        report = self._make_report(pass_count=2, fail_count=1)
        assert report.overall_status == "FAIL"

    def test_overall_pass(self):
        report = self._make_report(pass_count=3, fail_count=0)
        assert report.overall_status == "PASS"


# ---------------------------------------------------------------------------
# TopologyDiscovery (mock mode)
# ---------------------------------------------------------------------------
class TestTopologyDiscovery:

    def test_mock_discovery_creates_file(self, tmp_path):
        from standalone_tester.discovery.topology_discovery import TopologyDiscoveryAgent, GENERATED_DIR
        with patch("standalone_tester.discovery.topology_discovery.GENERATED_DIR", tmp_path):
            agent = TopologyDiscoveryAgent(mock=True)
            path = agent._mock_topology("test_lab")
            assert Path(path).exists()

    def test_discover_from_description_mock(self, tmp_path):
        from standalone_tester.discovery.topology_discovery import TopologyDiscoveryAgent
        with patch("standalone_tester.discovery.topology_discovery.GENERATED_DIR", tmp_path):
            agent = TopologyDiscoveryAgent(mock=True)
            path = agent.discover_from_description("2 Cisco PEs and 1 Nokia P", "test_lab")
            assert Path(path).exists()
