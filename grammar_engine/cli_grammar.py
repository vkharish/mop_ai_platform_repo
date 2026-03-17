"""
CLI Grammar Engine

Two roles in the pipeline:
  1. PRE-LLM:  Detect CLI commands in raw document text.
               Count is used as a guardrail baseline (expected_command_count).
  2. POST-LLM: Validate that commands in the canonical model are real CLI commands
               and enrich them with vendor/protocol/mode metadata.

Does NOT replace the LLM for step extraction — it only identifies CLI patterns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml


@dataclass
class DetectedCommand:
    """A CLI command detected by the grammar engine."""
    raw: str
    normalized: str
    vendor: Optional[str] = None
    protocol: Optional[str] = None
    mode: Optional[str] = None
    confidence: float = 0.5


class CLIGrammar:
    """
    Multi-vendor CLI grammar engine.

    Loads vendor and protocol patterns from protocol_patterns.yaml
    and applies them to detect, classify, and validate CLI commands.
    """

    def __init__(self, patterns_file: Optional[str] = None):
        if patterns_file is None:
            patterns_file = str(
                Path(__file__).parent.parent / "configs" / "protocol_patterns.yaml"
            )
        with open(patterns_file, "r") as f:
            self._patterns = yaml.safe_load(f)

        self._compiled = self._compile_patterns()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract_from_text(self, text: str) -> List[DetectedCommand]:
        """
        Extract all CLI commands from raw document text.
        Used PRE-LLM as a baseline count for guardrails.
        """
        commands: List[DetectedCommand] = []
        for line in text.splitlines():
            cmd = self._classify_line(line)
            if cmd:
                commands.append(cmd)
        return commands

    def enrich_command(self, raw: str) -> DetectedCommand:
        """
        Classify and enrich a single command string.
        Used POST-LLM to fill vendor/protocol/mode on LLM-extracted commands.
        """
        cmd = self._classify_line(raw)
        if cmd:
            return cmd
        # Return a low-confidence generic command
        return DetectedCommand(
            raw=raw,
            normalized=self._normalize(raw),
            vendor="generic",
            confidence=0.1,
        )

    def is_cli_command(self, text: str) -> bool:
        """Return True if the text looks like a CLI command."""
        return self._classify_line(text) is not None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _compile_patterns(self) -> Dict:
        """Pre-compile all regex patterns for performance."""
        compiled: Dict = {
            "vendors": {},
            "protocols": {},
            "generic": [],
            "prompt_strip": [],
        }

        for vendor_name, vendor_cfg in self._patterns.get("vendors", {}).items():
            compiled["vendors"][vendor_name] = {
                "exec": [],
                "config": [],
                "boost": vendor_cfg.get("confidence_boost", 0.0),
            }
            for entry in vendor_cfg.get("exec_commands", []):
                compiled["vendors"][vendor_name]["exec"].append(
                    re.compile(entry["pattern"], re.IGNORECASE)
                )
            for entry in vendor_cfg.get("config_commands", []):
                compiled["vendors"][vendor_name]["config"].append(
                    re.compile(entry["pattern"], re.IGNORECASE)
                )

        for proto_name, proto_cfg in self._patterns.get("protocols", {}).items():
            compiled["protocols"][proto_name] = {
                "keywords": proto_cfg.get("keywords", []),
                "commands": [c.lower() for c in proto_cfg.get("commands", [])],
            }

        for entry in self._patterns.get("generic_cli_indicators", []):
            compiled["generic"].append(re.compile(entry["pattern"], re.IGNORECASE))

        for entry in self._patterns.get("cli_prompt_patterns", []):
            compiled["prompt_strip"].append(re.compile(entry["pattern"]))

        return compiled

    def _classify_line(self, line: str) -> Optional[DetectedCommand]:
        """
        Classify a single line of text as a CLI command or None.

        Scoring:
        - Generic CLI indicator match: base confidence 0.5
        - Vendor-specific match: adds vendor confidence boost
        - Protocol keyword in command: adds 0.1
        """
        line = line.strip()
        if not line or len(line) < 3:
            return None

        # Strip CLI prompts (e.g., "PE1# show ip bgp")
        stripped = self._strip_prompt(line)

        if not stripped:
            return None

        # Try vendor-specific patterns first (higher confidence)
        vendor, mode, vendor_confidence = self._match_vendor(stripped)

        # Check generic patterns if no vendor match
        if vendor is None:
            if not self._matches_generic(stripped):
                return None
            base_confidence = 0.5
        else:
            base_confidence = 0.7 + vendor_confidence

        # Detect protocol
        protocol = self._detect_protocol(stripped)
        if protocol:
            base_confidence = min(1.0, base_confidence + 0.1)

        return DetectedCommand(
            raw=line,
            normalized=self._normalize(stripped),
            vendor=vendor or "generic",
            protocol=protocol,
            mode=mode,
            confidence=round(min(1.0, base_confidence), 2),
        )

    def _match_vendor(self, text: str) -> Tuple[Optional[str], Optional[str], float]:
        """Return (vendor, mode, confidence_boost) for first matching vendor pattern."""
        for vendor_name, patterns in self._compiled["vendors"].items():
            for pattern in patterns["exec"]:
                if pattern.match(text):
                    return vendor_name, "exec", patterns["boost"]
            for pattern in patterns["config"]:
                if pattern.match(text):
                    return vendor_name, "config", patterns["boost"]
        return None, None, 0.0

    def _matches_generic(self, text: str) -> bool:
        for pattern in self._compiled["generic"]:
            if pattern.match(text):
                return True
        return False

    def _detect_protocol(self, text: str) -> Optional[str]:
        text_lower = text.lower()

        # Pass 1: command prefix matching (highest confidence — check all protocols first)
        for proto_name, proto_cfg in self._compiled["protocols"].items():
            for cmd in proto_cfg["commands"]:
                if text_lower.startswith(cmd):
                    return proto_name

        # Pass 2: keyword matching (lower confidence — skip generic/ambiguous keywords)
        _SKIP_KEYWORDS = {"neighbor", "area", "level", "mode"}  # appear in multiple protocols
        for proto_name, proto_cfg in self._compiled["protocols"].items():
            for keyword in proto_cfg["keywords"]:
                if keyword in _SKIP_KEYWORDS:
                    continue
                if re.search(r"\b" + re.escape(keyword) + r"\b", text_lower):
                    return proto_name

        return None

    def _strip_prompt(self, text: str) -> str:
        """Remove CLI prompts like 'PE1#' or 'router>' from command lines."""
        for pattern in self._compiled["prompt_strip"]:
            text = pattern.sub("", text, count=1)
        return text.strip()

    @staticmethod
    def _normalize(text: str) -> str:
        """Normalize a command: lowercase, collapse whitespace."""
        return re.sub(r"\s+", " ", text.lower().strip())
