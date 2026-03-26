"""
Version Detector — identifies vendor/model/OS from 'show version' output.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class DetectedDevice:
    vendor: str
    os: str
    model: str
    version: str
    hostname: str


class VersionDetector:

    @staticmethod
    def detect(output: str) -> DetectedDevice:
        output_l = output.lower()

        # Cisco IOS-XR
        if "ios xr" in output_l or "ios-xr" in output_l:
            version = VersionDetector._extract(output, r"Version\s+(\S+)", "unknown")
            model = VersionDetector._extract(output, r"cisco\s+(ASR|NCS|CRS)[\w\-]+", "ASR9000")
            hostname = VersionDetector._extract(output, r"(\S+)\s+uptime", "unknown")
            return DetectedDevice("cisco", "ios-xr", model, version, hostname)

        # Cisco IOS-XE
        if "ios-xe" in output_l or "ios xe" in output_l:
            version = VersionDetector._extract(output, r"Version\s+([\d\.]+)", "unknown")
            model = VersionDetector._extract(output, r"cisco\s+(ISR|ASR|CSR)[\w\-]+", "unknown")
            hostname = VersionDetector._extract(output, r"(\S+)\s+uptime", "unknown")
            return DetectedDevice("cisco", "ios-xe", model, version, hostname)

        # Cisco NX-OS
        if "nx-os" in output_l or "nxos" in output_l:
            version = VersionDetector._extract(output, r"NXOS:\s+version\s+(\S+)", "unknown")
            model = VersionDetector._extract(output, r"cisco\s+(Nexus)[\w\s]+", "Nexus9K")
            hostname = VersionDetector._extract(output, r"Device name:\s+(\S+)", "unknown")
            return DetectedDevice("cisco", "nxos", model, version, hostname)

        # Juniper Junos
        if "junos" in output_l or "juniper" in output_l:
            version = VersionDetector._extract(output, r"JUNOS\s+(\S+)", "unknown")
            model = VersionDetector._extract(output, r"Model:\s+(\S+)", "MX")
            hostname = VersionDetector._extract(output, r"Hostname:\s+(\S+)", "unknown")
            return DetectedDevice("juniper", "junos", model, version, hostname)

        # Nokia SR OS
        if "nokia" in output_l or "sr os" in output_l or "timos" in output_l:
            version = VersionDetector._extract(output, r"TiMOS-[BCMP]\-(\S+)", "unknown")
            model = VersionDetector._extract(output, r"(7[2457]\d\d[- ]\S+)", "7750-SR")
            hostname = VersionDetector._extract(output, r"System Name\s*:\s*(\S+)", "unknown")
            return DetectedDevice("nokia", "sros", model, version, hostname)

        # Arista EOS
        if "arista" in output_l or "eos" in output_l:
            version = VersionDetector._extract(output, r"EOS version:\s+(\S+)", "unknown")
            model = VersionDetector._extract(output, r"Arista\s+(DCS-[\w\-]+)", "7280")
            hostname = VersionDetector._extract(output, r"Hostname:\s+(\S+)", "unknown")
            return DetectedDevice("arista", "eos", model, version, hostname)

        # Huawei VRP
        if "huawei" in output_l or "vrp" in output_l:
            version = VersionDetector._extract(output, r"VRP.*?V(\S+)", "unknown")
            model = VersionDetector._extract(output, r"Huawei\s+(NE\w+)", "NE40E")
            hostname = VersionDetector._extract(output, r"<(\S+)>", "unknown")
            return DetectedDevice("huawei", "vrp", model, version, hostname)

        return DetectedDevice("unknown", "unknown", "unknown", "unknown", "unknown")

    @staticmethod
    def _extract(text: str, pattern: str, default: str) -> str:
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else default
