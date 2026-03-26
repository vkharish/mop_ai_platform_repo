"""Structured result models for protocol test runs."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime


@dataclass
class TestResult:
    test_id: str
    intent: str
    severity: str
    status: str          # PASS | FAIL | SKIP | ERROR
    command: str = ""
    output: str = ""
    failure_reason: str = ""
    duration_ms: int = 0


@dataclass
class DeviceTestReport:
    device_name: str
    vendor: str
    os: str
    version: str
    host: str
    protocol: str
    test_type: str
    results: List[TestResult] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    completed_at: str = ""
    reachable: bool = True
    error: str = ""

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == "PASS")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "FAIL")

    @property
    def critical_failures(self) -> List[TestResult]:
        return [r for r in self.results if r.status == "FAIL" and r.severity == "critical"]

    @property
    def overall_status(self) -> str:
        if not self.reachable:
            return "UNREACHABLE"
        if self.critical_failures:
            return "FAIL"
        if self.failed > 0:
            return "WARN"
        return "PASS"

    def print_report(self) -> None:
        status_icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️ ", "UNREACHABLE": "🔌"}.get(self.overall_status, "?")
        print()
        print("=" * 66)
        print(f"  PROTOCOL TEST RESULTS  {status_icon} {self.overall_status}")
        print(f"  Device   : {self.device_name}  ({self.vendor} / {self.os} {self.version})")
        print(f"  Protocol : {self.protocol}  |  Type: {self.test_type}")
        print(f"  Host     : {self.host}")
        print("=" * 66)
        if not self.reachable:
            print(f"  ❌ Device unreachable: {self.error}")
        else:
            for r in self.results:
                icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭ ", "ERROR": "💥"}.get(r.status, "?")
                print(f"  {icon} [{r.severity.upper():<8}] {r.test_id:<30} {r.status}")
                if r.failure_reason:
                    print(f"       Reason: {r.failure_reason}")
            print(f"\n  Summary: {self.passed} passed, {self.failed} failed out of {len(self.results)} tests")
        print("=" * 66)


@dataclass
class TestSuiteReport:
    topology_name: str
    protocol: str
    test_type: str
    device_reports: List[DeviceTestReport] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    completed_at: str = ""

    @property
    def overall_status(self) -> str:
        if any(r.overall_status == "FAIL" for r in self.device_reports):
            return "FAIL"
        if any(r.overall_status in ("WARN", "UNREACHABLE") for r in self.device_reports):
            return "WARN"
        return "PASS"

    def print_summary(self) -> None:
        status_icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️ "}.get(self.overall_status, "?")
        print()
        print("=" * 66)
        print(f"  SUITE SUMMARY  {status_icon} {self.overall_status}")
        print(f"  Topology : {self.topology_name}")
        print(f"  Protocol : {self.protocol}  |  Type: {self.test_type}")
        print("=" * 66)
        for r in self.device_reports:
            icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️ ", "UNREACHABLE": "🔌"}.get(r.overall_status, "?")
            print(f"  {icon} {r.device_name:<12} ({r.vendor}/{r.os})  — {r.passed}/{len(r.results)} passed")
        print(f"\n  Overall: {self.overall_status}")
        print("=" * 66)
