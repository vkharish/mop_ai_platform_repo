"""
Inventory Manager — loads device inventory from the structured folder layout.

Resolves vendor model references and merges template defaults
with device-specific overrides.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


INVENTORY_ROOT = Path(__file__).parent.parent / "inventory"


class ResolvedDevice:
    """Fully resolved device — vendor template merged with topology instance."""

    def __init__(self, name: str, data: dict):
        self.name = name
        self.vendor: str = data.get("vendor", "unknown")
        self.os: str = data.get("os", "unknown")
        self.model: str = data.get("model", "unknown")
        self.version: str = data.get("version", "unknown")
        self.role: str = data.get("role", "unknown")
        self.host: str = data.get("connection", {}).get("host", "")
        self.port: int = data.get("connection", {}).get("port", 22)
        self.credentials_env: str = data.get("credentials_env", "LAB_CREDS")
        self.capabilities: dict = data.get("capabilities", {})
        self.commands: dict = data.get("commands", {})
        self.quirks: dict = data.get("quirks", {})

    @property
    def username(self) -> str:
        env = self.credentials_env
        return os.environ.get(f"{env}_USER", os.environ.get("LAB_USER", "admin"))

    @property
    def password(self) -> str:
        env = self.credentials_env
        return os.environ.get(f"{env}_PASS", os.environ.get("LAB_PASS", ""))

    def supports_protocol(self, protocol: str) -> bool:
        return protocol in self.capabilities.get("protocols", [])

    def __repr__(self) -> str:
        return f"Device({self.name}, {self.vendor}/{self.os}, {self.host})"


class InventoryManager:

    def __init__(self, vendors_root: Optional[Path] = None):
        self._vendors_root = vendors_root or (INVENTORY_ROOT / "vendors")
        self._template_cache: dict = {}

    def load_topology(self, topology_path: str) -> Dict[str, ResolvedDevice]:
        """Load a topology file and return fully resolved devices."""
        path = Path(topology_path)
        if not path.is_absolute():
            # try relative to inventory/topologies
            path = INVENTORY_ROOT / "topologies" / topology_path
        if not path.exists():
            raise FileNotFoundError(f"Topology not found: {path}")

        with open(path, encoding="utf-8") as f:
            topo = yaml.safe_load(f)

        devices = {}
        for device_name, device_data in (topo.get("devices") or {}).items():
            ref = device_data.get("ref", "")
            resolved = self._resolve(device_name, device_data, ref)
            devices[device_name] = resolved

        return devices

    def load_generated(self, filename: str) -> Dict[str, ResolvedDevice]:
        """Load an auto-generated inventory file."""
        path = INVENTORY_ROOT / "generated" / filename
        return self.load_topology(str(path))

    def filter_by_role(
        self, devices: Dict[str, ResolvedDevice], role: str
    ) -> Dict[str, ResolvedDevice]:
        return {n: d for n, d in devices.items() if d.role == role}

    def filter_by_vendor(
        self, devices: Dict[str, ResolvedDevice], vendor: str
    ) -> Dict[str, ResolvedDevice]:
        return {n: d for n, d in devices.items() if d.vendor == vendor}

    def filter_by_protocol(
        self, devices: Dict[str, ResolvedDevice], protocol: str
    ) -> Dict[str, ResolvedDevice]:
        return {n: d for n, d in devices.items() if d.supports_protocol(protocol)}

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _resolve(self, name: str, instance_data: dict, ref: str) -> ResolvedDevice:
        """Merge vendor defaults + model config + instance overrides."""
        merged: dict = {}

        if ref:
            parts = ref.strip("/").split("/")
            if len(parts) >= 2:
                vendor, os_type = parts[0], parts[1]
                model_file = parts[2] if len(parts) > 2 else None

                # Load OS defaults
                defaults = self._load_template(vendor, os_type, "_defaults")
                merged.update(defaults)

                # Load model-specific config
                if model_file:
                    model_cfg = self._load_template(vendor, os_type, model_file)
                    # Deep merge capabilities
                    if "capabilities" in model_cfg and "capabilities" in merged:
                        merged["capabilities"]["protocols"] = list(set(
                            merged["capabilities"].get("protocols", []) +
                            model_cfg["capabilities"].get("protocols", [])
                        ))
                        merged["capabilities"]["features"] = list(set(
                            merged["capabilities"].get("features", []) +
                            model_cfg["capabilities"].get("features", [])
                        ))
                    merged.update({k: v for k, v in model_cfg.items() if k != "capabilities"})

        # Instance data overrides (version, role, connection, credentials_env)
        for key in ("version", "role", "connection", "credentials_env"):
            if key in instance_data:
                merged[key] = instance_data[key]

        merged["name"] = name
        return ResolvedDevice(name, merged)

    def _load_template(self, vendor: str, os_type: str, filename: str) -> dict:
        cache_key = f"{vendor}/{os_type}/{filename}"
        if cache_key in self._template_cache:
            return dict(self._template_cache[cache_key])

        path = self._vendors_root / vendor / os_type / f"{filename}.yaml"
        if not path.exists():
            return {}

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        self._template_cache[cache_key] = data
        return dict(data)
