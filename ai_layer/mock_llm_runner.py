"""
Mock LLM Runner — offline testing without an Anthropic API key.

Produces a realistic CanonicalTestModel from the grammar-engine's pre-detected
commands, without making any network calls.  Every post-LLM pipeline stage
(guardrails, schema validation, generators) runs normally so you can verify
the full pipeline end-to-end.

How the stub works:
  - Detected commands are grouped into 4 canonical sections:
    Pre-checks, Implementation, Verification, Rollback
  - Section assignment is heuristic (keywords in the command text).
  - Each command becomes one TestStep with appropriate step_type / action_type.
  - Steps without commands become INFO steps.

Usage (via pipeline.py):
    python pipeline.py --input mop.pdf --output ./out --mock-llm
"""

from __future__ import annotations

import re
import time
import uuid
from typing import List, Optional

from grammar_engine.cli_grammar import DetectedCommand
from models.canonical import (
    ActionType,
    CanonicalTestModel,
    CLICommand,
    StepType,
    TestStep,
)
from ai_layer.llm_result import LLMResult


# ---------------------------------------------------------------------------
# Section heuristics
# ---------------------------------------------------------------------------

_PRE_CHECK_HINTS = re.compile(
    r"\b(show|display|verify|check|ping|traceroute|get|status|state|baseline|confirm|validate)\b",
    re.IGNORECASE,
)
_ROLLBACK_HINTS = re.compile(
    r"\b(rollback|undo|revert|remove|delete|no\s+|restore|deactivate|shutdown)\b",
    re.IGNORECASE,
)
_VERIFY_HINTS = re.compile(
    r"\b(show|display|ping|traceroute|verify|check|test|confirm|assert|expected|after|post)\b",
    re.IGNORECASE,
)


def _guess_section(cmd_raw: str, seq: int, total: int) -> tuple[str, StepType, ActionType]:
    """
    Assign a section, StepType, and ActionType from the command text and position.

    Positional heuristic (rough thirds):
      - first 20% → Pre-checks
      - last 15% → Rollback
      - middle → Implementation / Verification
    Command-text heuristic overrides position.
    """
    fraction = seq / max(total, 1)

    if _ROLLBACK_HINTS.search(cmd_raw):
        return "Rollback", StepType.ROLLBACK, ActionType.ROLLBACK

    if fraction <= 0.20 and _PRE_CHECK_HINTS.search(cmd_raw):
        return "Pre-checks", StepType.VERIFICATION, ActionType.VERIFY

    if fraction >= 0.75 and _VERIFY_HINTS.search(cmd_raw):
        return "Verification", StepType.VERIFICATION, ActionType.VERIFY

    if _VERIFY_HINTS.search(cmd_raw) and "show" in cmd_raw.lower():
        section = "Pre-checks" if fraction < 0.4 else "Verification"
        return section, StepType.VERIFICATION, ActionType.VERIFY

    return "Implementation", StepType.ACTION, ActionType.EXECUTE


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_mock(
    doc_title: str,
    source_file: str,
    source_format: str,
    mop_structure: str,
    detected_commands: List[DetectedCommand],
) -> LLMResult:
    """
    Build a CanonicalTestModel from grammar-detected commands and return
    it wrapped in a successful LLMResult — no API call made.

    Args:
        doc_title:         Document title from the ingestion layer.
        source_file:       Source file path.
        source_format:     pdf | docx | txt
        mop_structure:     Detected MOP structure string.
        detected_commands: Commands from CLIGrammar.extract_from_text().

    Returns:
        LLMResult(success=True) with a fully populated CanonicalTestModel.
    """
    t0 = time.time()
    steps: List[TestStep] = []
    total = max(len(detected_commands), 1)

    # Section-aware deduplication:
    # The same command is legitimate in multiple sections — e.g. "show bgp summary"
    # as a pre-check baseline AND again as a post-change verification are two
    # distinct steps that happen to use the same CLI command.
    # Only remove a command if the exact same command has already appeared in the
    # SAME section (true accidental duplicates within one phase of the procedure).
    seen_per_section: dict[str, set[str]] = {}
    unique_cmds: List[DetectedCommand] = []
    for idx0, c in enumerate(detected_commands, start=1):
        section0, _, _ = _guess_section(c.raw, idx0, total)
        key = c.normalized or c.raw.lower().strip()
        if section0 not in seen_per_section:
            seen_per_section[section0] = set()
        if key not in seen_per_section[section0]:
            seen_per_section[section0].add(key)
            unique_cmds.append(c)

    for idx, dc in enumerate(unique_cmds, start=1):
        section, step_type, action_type = _guess_section(dc.raw, idx, len(unique_cmds))
        is_rollback = section == "Rollback"

        cli_cmd = CLICommand(
            raw=dc.raw,
            normalized=dc.normalized,
            vendor=dc.vendor,
            protocol=dc.protocol,
            mode=dc.mode,
            confidence=dc.confidence,
        )

        description = _make_description(dc, action_type)

        steps.append(TestStep(
            step_id=str(uuid.uuid4())[:8],
            sequence=idx,
            step_type=step_type,
            action_type=action_type,
            description=description,
            raw_text=dc.raw,
            commands=[cli_cmd],
            expected_output=_guess_expected(dc, action_type),
            section=section,
            subsection=None,
            is_rollback=is_rollback,
            tags=_make_tags(dc),
        ))

    # If no commands were detected, add a single placeholder INFO step
    if not steps:
        steps.append(TestStep(
            step_id=str(uuid.uuid4())[:8],
            sequence=1,
            step_type=StepType.INFO,
            action_type=ActionType.OBSERVE,
            description="No CLI commands detected in document — manual review required",
            raw_text="(No commands detected)",
            section="General",
        ))

    model = CanonicalTestModel(
        document_title=doc_title,
        source_file=source_file,
        source_format=source_format,
        mop_structure=mop_structure,
        steps=steps,
        metadata={
            "mock_llm": True,
            "mock_note": (
                "Generated by mock LLM runner — no API call made. "
                "Steps derived from grammar-engine command detection only."
            ),
            "commands_detected": len(detected_commands),
            "commands_after_dedup": len(unique_cmds),
            "dedup_note": "Duplicates removed only within the same section; same command in pre-checks and verification is preserved as two distinct steps.",
        },
    )

    elapsed_ms = int((time.time() - t0) * 1000)

    return LLMResult(
        success=True,
        model=model,
        attempt_count=1,
        chunk_count=1,
        latency_ms=elapsed_ms,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _make_description(dc: DetectedCommand, action_type: ActionType) -> str:
    """Generate a human-readable step description from a detected command."""
    verb = {
        ActionType.VERIFY: "Verify",
        ActionType.ROLLBACK: "Rollback",
        ActionType.CONFIGURE: "Configure",
        ActionType.EXECUTE: "Execute",
        ActionType.OBSERVE: "Observe",
    }.get(action_type, "Execute")

    proto = f" ({dc.protocol.upper()})" if dc.protocol else ""
    vendor = f" [{dc.vendor}]" if dc.vendor and dc.vendor != "generic" else ""
    cmd_short = dc.raw[:60] + ("…" if len(dc.raw) > 60 else "")
    return f"{verb}{proto}{vendor}: {cmd_short}"


def _guess_expected(dc: DetectedCommand, action_type: ActionType) -> Optional[str]:
    """Return a plausible expected output hint for verification steps."""
    if action_type != ActionType.VERIFY:
        return None
    cmd = dc.raw.lower()
    if "bgp" in cmd:
        return "BGP neighbors in Established state"
    if "ospf" in cmd:
        return "OSPF neighbors in FULL state"
    if "isis" in cmd:
        return "IS-IS adjacencies in UP state"
    if "interface" in cmd or "ip int" in cmd:
        return "Interface state: up/up"
    if "install" in cmd and "active" in cmd:
        return "SMU listed in active packages"
    if "install" in cmd:
        return "Install operation completed successfully"
    if "ping" in cmd:
        return "Success rate 100% (no packet loss)"
    if "version" in cmd:
        return "Expected software version shown"
    return "Command completes without errors"


def _make_tags(dc: DetectedCommand) -> List[str]:
    tags = []
    if dc.vendor and dc.vendor != "generic":
        tags.append(dc.vendor)
    if dc.protocol:
        tags.append(dc.protocol)
    cmd = dc.raw.lower()
    if "install" in cmd:
        tags.append("smu")
    if "rollback" in cmd or "undo" in cmd or "no " in cmd:
        tags.append("rollback")
    if "show" in cmd or "display" in cmd:
        tags.append("read-only")
    return tags
