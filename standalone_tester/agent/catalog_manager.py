"""
Catalog Manager — loads and queries the test catalog.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import yaml


CATALOG_PATH = Path(__file__).parent.parent / "test_catalog" / "catalog.yaml"


class CatalogEntry:
    def __init__(self, data: dict):
        self.id: str = data.get("id", "")
        self.intent: str = data.get("intent", "")
        self.severity: str = data.get("severity", "medium")
        self.sla_seconds: float = data.get("sla_seconds", 0)
        self.sla_ms: float = data.get("sla_ms", 0)
        self.threshold_pct: float = data.get("threshold_pct", 0)

    def __repr__(self) -> str:
        return f"CatalogEntry({self.id}: {self.intent[:40]})"


class CatalogManager:

    def __init__(self, catalog_path: str = None):
        path = Path(catalog_path) if catalog_path else CATALOG_PATH
        with open(path, encoding="utf-8") as f:
            self._catalog = yaml.safe_load(f) or {}

    def get_tests(self, protocol: str, test_type: str) -> List[CatalogEntry]:
        """Return tests for a given protocol and test type."""
        protocol_data = self._catalog.get("protocols", {}).get(protocol, {})
        tests = protocol_data.get(test_type, [])
        return [CatalogEntry(t) for t in tests]

    def supported_protocols(self) -> List[str]:
        return list(self._catalog.get("protocols", {}).keys())

    def supported_test_types(self, protocol: str) -> List[str]:
        return list(self._catalog.get("protocols", {}).get(protocol, {}).keys())
