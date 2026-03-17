"""
Canonical data models for the MOP AI Platform.

These are the shared contracts between all pipeline stages:
  ingestion → (canonical) → ai_layer → (canonical) → generators

Nothing in the generators knows about PDF, DOCX, or LLM specifics.
Everything speaks CanonicalTestModel.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class StepType(str, Enum):
    ACTION = "action"           # operator does something (configure, execute)
    VERIFICATION = "verification"  # operator checks something
    ROLLBACK = "rollback"       # backout step
    CONFIG = "config"           # pure configuration push
    INFO = "info"               # informational / note, no operator action


class ActionType(str, Enum):
    EXECUTE = "execute"         # run a command / script
    VERIFY = "verify"           # verify output or state
    CONFIGURE = "configure"     # push configuration
    ROLLBACK = "rollback"       # undo a change
    OBSERVE = "observe"         # passive observation / logging


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class CLICommand(BaseModel):
    """A single CLI command extracted from a MOP step."""

    raw: str
    """The command exactly as it appeared in the MOP document."""

    normalized: Optional[str] = None
    """Canonicalized form (lowercase, whitespace-collapsed). Populated by grammar engine."""

    vendor: Optional[str] = None
    """Detected vendor: cisco | juniper | nokia | arista | generic"""

    protocol: Optional[str] = None
    """Detected protocol/technology: bgp | ospf | isis | mpls | interface | system | etc."""

    mode: Optional[str] = None
    """CLI mode: exec | config | null"""

    confidence: float = 1.0
    """Grammar engine confidence that this is a CLI command (0.0–1.0)."""


class TestStep(BaseModel):
    """A single actionable step extracted from a MOP document."""

    step_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    sequence: int

    step_type: StepType
    action_type: ActionType = ActionType.EXECUTE

    description: str
    """Human-readable description of what this step does."""

    raw_text: str
    """The original verbatim text of this step from the source document."""

    commands: List[CLICommand] = []
    """CLI commands associated with this step."""

    expected_output: Optional[str] = None
    """The expected output or verification criteria, if mentioned."""

    section: Optional[str] = None
    """Top-level section this step belongs to (e.g., 'Pre-checks', 'Implementation')."""

    subsection: Optional[str] = None
    """Sub-section, if any (e.g., 'Step 3 – BGP Configuration')."""

    is_rollback: bool = False
    """True if this step is part of a rollback / backout procedure."""

    tags: List[str] = []
    """Free-form tags added by grammar engine or LLM (e.g., 'bgp', 'cisco', 'critical')."""


# ---------------------------------------------------------------------------
# Top-level model
# ---------------------------------------------------------------------------

class CanonicalTestModel(BaseModel):
    """
    The central contract model that flows through the entire pipeline.

    Produced by: ai_layer.super_prompt_runner (after LLM processing)
    Consumed by: generators.zephyr_generator, generators.robot_generator,
                 generators.cli_rule_generator
    """

    document_title: str
    source_file: str
    source_format: str
    """File format: pdf | docx | txt"""

    mop_structure: str = "unknown"
    """
    Detected MOP structure before LLM processing.
    Values: numbered_list | bulleted_list | table | prose | mixed | unknown
    This is metadata only — the LLM handles all structures uniformly.
    """

    steps: List[TestStep]

    metadata: Dict[str, Any] = {}
    """
    Arbitrary metadata: vendor hints, processing timestamps, guardrail results, etc.
    """


# ---------------------------------------------------------------------------
# Document ingestion models (used by ingestion layer only)
# ---------------------------------------------------------------------------

@dataclass
class DocumentBlock:
    """
    A structural unit extracted from a source document.
    The ingestion layer produces these; they are fed to the LLM as raw text.
    """

    block_type: str
    """paragraph | heading | list_item | table_row | code_block | caption"""

    content: str
    """Text content of this block."""

    level: int = 0
    """Heading level (1–6) or list nesting depth."""

    row_index: int = -1
    """For table rows: 0 = header row, >0 = data row."""

    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedDocument:
    """
    Output of the ingestion layer — a document broken into typed blocks.
    """

    title: str
    source_file: str
    source_format: str

    blocks: List[DocumentBlock]

    full_text: str
    """Concatenated plain text of all blocks (used as LLM input)."""

    detected_structure: str = "unknown"
    """
    Best-guess structure detected by the normalizer.
    Used only as a hint in the LLM prompt.
    """
