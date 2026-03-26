"""
Topology Discovery Agent

Connects to a seed device, uses LLDP/CDP to discover all neighbors,
runs 'show version' on each, and generates a topology YAML file.

Two modes:
  1. Live  — SSH to real devices via LLDP/CDP
  2. Describe — plain English topology description via LLM
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from standalone_tester.discovery.version_detector import DetectedDevice, VersionDetector

logger = logging.getLogger(__name__)

GENERATED_DIR = Path(__file__).parent.parent / "inventory" / "generated"
GENERATED_DIR.mkdir(exist_ok=True)

_VENDOR_TO_REF = {
    ("cisco",   "ios-xr"): "cisco/ios-xr/asr9000",
    ("cisco",   "ios-xe"): "cisco/ios-xe/asr1000",
    ("cisco",   "nxos"):   "cisco/nxos/nexus9k",
    ("juniper", "junos"):  "juniper/junos/mx-series",
    ("nokia",   "sros"):   "nokia/sros/7750-sr",
    ("arista",  "eos"):    "arista/eos/7280",
    ("huawei",  "vrp"):    "huawei/vrp/ne40e",
    ("ericsson","ipos"):   "ericsson/ipos/smartedge",
}


class TopologyDiscoveryAgent:

    def __init__(self, mock: bool = False):
        self._mock = mock

    def discover_live(
        self,
        seed_host: str,
        username: str,
        password: str,
        depth: int = 2,
        topology_name: str = "",
    ) -> str:
        """
        SSH to seed device, discover neighbors via LLDP/CDP, build topology YAML.
        Returns path to generated topology file.
        """
        logger.info("Starting topology discovery from seed: %s", seed_host)
        discovered: Dict[str, dict] = {}
        visited_hosts = set()

        self._discover_recursive(
            host=seed_host,
            username=username,
            password=password,
            depth=depth,
            discovered=discovered,
            visited=visited_hosts,
        )

        return self._save_topology(discovered, topology_name or f"discovered_{seed_host}")

    def discover_from_description(
        self,
        description: str,
        topology_name: str = "",
        api_key: Optional[str] = None,
    ) -> str:
        """
        Use LLM to generate topology YAML from plain English description.
        Returns path to generated topology file.
        """
        if self._mock:
            return self._mock_topology(topology_name)

        try:
            import anthropic
            key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
            client = anthropic.Anthropic(api_key=key)
            prompt = f"""You are a network engineer. Generate a network topology YAML from this description:

"{description}"

Return ONLY valid YAML in this exact format:
```yaml
name: <topology name>
description: <description>
devices:
  <DEVICE_NAME>:
    ref: <vendor>/<os>/<model>   # e.g. cisco/ios-xr/asr9000
    version: "<version>"
    role: <pe-router|p-router|route-reflector|ce-router>
    connection:
      host: "<ip or placeholder like 192.168.x.x>"
      port: 22
    credentials_env: <DEVICE_NAME>_CREDS
links:
  - from: <DEV1>  to: <DEV2>  protocol: <protocols>
```

Valid refs: cisco/ios-xr/asr9000, cisco/ios-xe/asr1000, juniper/junos/mx-series, nokia/sros/7750-sr, arista/eos/7280, huawei/vrp/ne40e, ericsson/ipos/smartedge"""

            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text
            m = re.search(r'```yaml\s*(.*?)\s*```', text, re.DOTALL)
            yaml_text = m.group(1) if m else text
            topo = yaml.safe_load(yaml_text)
        except Exception as e:
            logger.error("LLM topology generation failed: %s", e)
            return self._mock_topology(topology_name)

        name = topology_name or topo.get("name", "described_topology")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = re.sub(r"[^\w\-]", "_", name)
        out_path = GENERATED_DIR / f"{safe_name}_{ts}.yaml"
        with open(out_path, "w", encoding="utf-8") as f:
            yaml.dump(topo, f, default_flow_style=False)

        logger.info("Generated topology saved: %s", out_path)
        return str(out_path)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _discover_recursive(
        self,
        host: str,
        username: str,
        password: str,
        depth: int,
        discovered: dict,
        visited: set,
    ) -> None:
        if host in visited or depth < 0:
            return
        visited.add(host)

        try:
            driver = self._connect(host, username, password)
            if not driver:
                return

            version_output = driver.execute("show version")
            detected = VersionDetector.detect(version_output)

            device_name = detected.hostname if detected.hostname != "unknown" else f"device_{host.replace('.', '_')}"
            ref = _VENDOR_TO_REF.get((detected.vendor, detected.os), "cisco/ios-xr/asr9000")

            discovered[device_name] = {
                "ref": ref,
                "version": detected.version,
                "role": "unknown",
                "connection": {"host": host, "port": 22},
                "credentials_env": f"{device_name.upper()}_CREDS",
                "_detected": {
                    "vendor": detected.vendor,
                    "os": detected.os,
                    "model": detected.model,
                },
            }
            logger.info("Discovered: %s → %s/%s %s (%s)",
                        device_name, detected.vendor, detected.os, detected.version, host)

            if depth > 0:
                neighbors = self._get_neighbors(driver, detected.vendor)
                driver.close()
                for neighbor_host in neighbors:
                    if neighbor_host not in visited:
                        self._discover_recursive(
                            neighbor_host, username, password,
                            depth - 1, discovered, visited
                        )
            else:
                driver.close()

        except Exception as e:
            logger.warning("Discovery failed for %s: %s", host, e)

    def _connect(self, host, username, password):
        if self._mock:
            from device_layer.device_driver import MockDriver
            drv = MockDriver(host)
            drv.connect()
            return drv
        try:
            from device_layer.device_driver import NetmikoDriver
            d = NetmikoDriver(hostname=host, username=username,
                              password=password, vendor="cisco")
            d.connect()
            return d
        except Exception as e:
            logger.error("Cannot connect to %s: %s", host, e)
            return None

    def _get_neighbors(self, driver, vendor: str) -> List[str]:
        cmd_map = {
            "cisco":   "show lldp neighbors detail",
            "juniper": "show lldp neighbors",
            "nokia":   "show system lldp neighbor",
            "arista":  "show lldp neighbors detail",
            "huawei":  "display lldp neighbor brief",
        }
        try:
            output = driver.execute(cmd_map.get(vendor, "show lldp neighbors"))
            # Extract IP addresses from LLDP output
            ips = re.findall(r'Management Addresses.*?(\d{1,3}(?:\.\d{1,3}){3})', output, re.DOTALL)
            if not ips:
                ips = re.findall(r'(\d{1,3}(?:\.\d{1,3}){3})', output)
            return list(set(ips))
        except Exception:
            return []

    def _save_topology(self, discovered: dict, name: str) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = re.sub(r"[^\w\-]", "_", name)
        out_path = GENERATED_DIR / f"{safe_name}_{ts}.yaml"

        topo = {
            "name": name,
            "description": f"Auto-discovered topology ({len(discovered)} devices)",
            "devices": {
                dev_name: {k: v for k, v in dev_data.items() if not k.startswith("_")}
                for dev_name, dev_data in discovered.items()
            },
        }
        with open(out_path, "w", encoding="utf-8") as f:
            yaml.dump(topo, f, default_flow_style=False)

        print(f"\n✅ Discovered {len(discovered)} device(s):")
        for name_d, data in discovered.items():
            det = data.get("_detected", {})
            print(f"   {name_d:<15} → {det.get('vendor','?')}/{det.get('os','?')} "
                  f"{data.get('version','?'):<12} ({data['connection']['host']})")
        print(f"\nSaved → {out_path}\n")
        return str(out_path)

    def _mock_topology(self, name: str) -> str:
        mock_topo = {
            "name": name or "mock_topology",
            "description": "Mock topology for testing",
            "devices": {
                "PE1": {
                    "ref": "cisco/ios-xr/asr9000",
                    "version": "7.5.1",
                    "role": "pe-router",
                    "connection": {"host": "192.168.1.1", "port": 22},
                    "credentials_env": "PE1_CREDS",
                },
                "RR1": {
                    "ref": "juniper/junos/mx-series",
                    "version": "22.4R1",
                    "role": "route-reflector",
                    "connection": {"host": "192.168.1.2", "port": 22},
                    "credentials_env": "RR1_CREDS",
                },
            },
        }
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = GENERATED_DIR / f"mock_{ts}.yaml"
        with open(out_path, "w", encoding="utf-8") as f:
            yaml.dump(mock_topo, f, default_flow_style=False)
        return str(out_path)
