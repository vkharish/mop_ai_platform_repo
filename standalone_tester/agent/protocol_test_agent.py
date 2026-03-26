"""
Protocol Test Agent

The main agent that:
  1. Loads device from inventory
  2. Loads tests from catalog
  3. Translates each intent to vendor-specific command (LLM/cache)
  4. Executes commands via SSH (DeviceDriver)
  5. Validates output
  6. Returns structured DeviceTestReport

Differentiator from Claude Code skills:
  - Python agent = pattern match, structured JSON, automated/CI-CD
  - Claude Code skills = full LLM reasoning, conversational, interactive
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Dict, List, Optional

from standalone_tester.agent.catalog_manager import CatalogEntry, CatalogManager
from standalone_tester.agent.command_translator import CommandTranslator
from standalone_tester.agent.inventory_manager import InventoryManager, ResolvedDevice
from standalone_tester.agent.result_model import DeviceTestReport, TestResult, TestSuiteReport

logger = logging.getLogger(__name__)


class ProtocolTestAgent:
    """
    Python-based agent: cheap, fast, automated.
    Uses LLM only for command translation (cached after first run).
    Does NOT reason about unexpected output — that is Claude Code's job.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        mock_ssh: bool = False,
        mock_llm: bool = False,
    ):
        self._translator = CommandTranslator(api_key=api_key, mock=mock_llm)
        self._catalog = CatalogManager()
        self._inventory = InventoryManager()
        self._mock_ssh = mock_ssh

    def run(
        self,
        topology_path: str,
        protocol: str,
        test_type: str,
        device_filter: Optional[str] = None,
        vendor_filter: Optional[str] = None,
        role_filter: Optional[str] = None,
    ) -> TestSuiteReport:
        """Run protocol tests against a topology."""
        devices = self._inventory.load_topology(topology_path)

        # Apply filters
        if device_filter:
            names = [n.strip() for n in device_filter.split(",")]
            devices = {n: d for n, d in devices.items() if n in names}
        if vendor_filter:
            devices = self._inventory.filter_by_vendor(devices, vendor_filter)
        if role_filter:
            devices = self._inventory.filter_by_role(devices, role_filter)
        if protocol != "all":
            devices = self._inventory.filter_by_protocol(devices, protocol)

        protocols = self._catalog.supported_protocols() if protocol == "all" else [protocol]

        import os
        topo_name = os.path.basename(topology_path).replace(".yaml", "")
        suite = TestSuiteReport(
            topology_name=topo_name,
            protocol=protocol,
            test_type=test_type,
        )

        for device_name, device in devices.items():
            for proto in protocols:
                if not device.supports_protocol(proto):
                    continue
                report = self._test_device(device, proto, test_type)
                suite.device_reports.append(report)

        suite.completed_at = datetime.utcnow().isoformat()
        return suite

    def run_single(
        self,
        device: ResolvedDevice,
        protocol: str,
        test_type: str,
    ) -> DeviceTestReport:
        """Run tests on a single resolved device."""
        return self._test_device(device, protocol, test_type)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _test_device(
        self, device: ResolvedDevice, protocol: str, test_type: str
    ) -> DeviceTestReport:
        report = DeviceTestReport(
            device_name=device.name,
            vendor=device.vendor,
            os=device.os,
            version=device.version,
            host=device.host,
            protocol=protocol,
            test_type=test_type,
        )

        tests = self._catalog.get_tests(protocol, test_type)
        if not tests:
            logger.warning("No tests found for %s/%s", protocol, test_type)
            report.completed_at = datetime.utcnow().isoformat()
            return report

        driver = self._get_driver(device)
        if driver is None:
            report.reachable = False
            report.error = f"Cannot connect to {device.host}"
            report.completed_at = datetime.utcnow().isoformat()
            return report

        try:
            for test in tests:
                result = self._run_test(driver, device, test)
                report.results.append(result)
        finally:
            try:
                driver.close()
            except Exception:
                pass

        report.completed_at = datetime.utcnow().isoformat()
        return report

    def _run_test(self, driver, device: ResolvedDevice, test: CatalogEntry) -> TestResult:
        t0 = time.time()
        translated = self._translator.translate(
            intent=test.intent,
            intent_id=test.id,
            vendor=device.vendor,
            os_type=device.os,
            version=device.version,
        )

        result = TestResult(
            test_id=test.id,
            intent=test.intent,
            severity=test.severity,
            status="SKIP",
            command=translated.command,
        )

        try:
            output = driver.execute(translated.command)
            result.output = output[:2000]  # cap stored output
            result.status = self._validate(
                output, translated.success_pattern, translated.error_pattern, test
            )
            if result.status == "FAIL":
                result.failure_reason = self._extract_failure_reason(output, translated.error_pattern)
        except Exception as e:
            result.status = "ERROR"
            result.failure_reason = str(e)

        result.duration_ms = int((time.time() - t0) * 1000)
        return result

    def _validate(
        self,
        output: str,
        success_pattern: str,
        error_pattern: str,
        test: CatalogEntry,
    ) -> str:
        # Check error pattern first
        if error_pattern and re.search(error_pattern, output, re.IGNORECASE):
            return "FAIL"
        # Check success pattern
        if success_pattern and not re.search(success_pattern, output, re.IGNORECASE):
            return "FAIL"
        # Threshold checks
        if test.threshold_pct:
            nums = re.findall(r'\b(\d+(?:\.\d+)?)\s*%', output)
            if nums and float(nums[0]) > test.threshold_pct:
                return "FAIL"
        return "PASS"

    def _extract_failure_reason(self, output: str, error_pattern: str) -> str:
        if not error_pattern:
            return "Expected pattern not found in output"
        for line in output.splitlines():
            if re.search(error_pattern, line, re.IGNORECASE):
                return line.strip()[:120]
        return "Expected success pattern not found"

    def _get_driver(self, device: ResolvedDevice):
        if self._mock_ssh:
            from device_layer.device_driver import MockDriver
            drv = MockDriver(device.name)
            drv.connect()
            return drv
        try:
            from device_layer.device_driver import NetmikoDriver
            driver = NetmikoDriver(
                hostname=device.host,
                username=device.username,
                password=device.password,
                vendor=device.vendor,
                port=device.port,
            )
            driver.connect()
            return driver
        except Exception as e:
            logger.error("SSH connection failed to %s (%s): %s", device.name, device.host, e)
            return None

    @staticmethod
    def _netmiko_type(vendor: str, os_type: str) -> str:
        mapping = {
            ("cisco", "ios-xr"):  "cisco_xr",
            ("cisco", "ios-xe"):  "cisco_ios",
            ("cisco", "nxos"):    "cisco_nxos",
            ("juniper", "junos"): "juniper",
            ("nokia", "sros"):    "nokia_sros",
            ("arista", "eos"):    "arista_eos",
            ("huawei", "vrp"):    "huawei_vrp",
            ("ericsson", "ipos"): "ericsson_ipos",
        }
        return mapping.get((vendor, os_type), "cisco_ios")
