"""
TOON Data Models — Tree of Outlined Nodes

A TOON is a compact intermediate representation of a MOP document.
It sits between raw ingestion text and the LLM prompt, reducing
token usage by 85-90% for structured documents before LLM enrichment.

Raw MOP text for 200-page doc : ~400k tokens
TOON for same doc              : ~30-50k tokens
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class TOONNodeType(str, Enum):
    SECTION    = "section"       # Document section heading
    LIST_STEP  = "list_step"     # Step from a numbered or bullet list
    TABLE_STEP = "table_step"    # Step from a table row
    PROSE_STEP = "prose_step"    # Step from a significant prose sentence
    CODE_STEP  = "code_step"     # Step from a code block


@dataclass
class TOONNode:
    """
    A single node in the Tree of Outlined Nodes.

    Represents one actionable step in compressed form.
    CLI commands are verbatim; prose is trimmed to ≤120 chars.
    """

    node_type: TOONNodeType
    node_id: str
    """Hierarchical ID: s{section_index}.{step_index}, e.g. 's2.3'"""

    section: str
    """Parent section heading."""

    description: str
    """Compressed step description (≤120 chars). Filler phrases removed."""

    commands: List[str] = field(default_factory=list)
    """CLI commands verbatim — never compressed."""

    expected_output: Optional[str] = None
    """Expected result or verification criteria (≤120 chars), if found."""

    is_rollback: bool = False

    source_block_type: str = ""
    """Original block type: list_item | table_row | paragraph | code_block"""

    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TOONSection:
    """A section of the document with its TOONNodes."""

    heading: str
    section_index: int
    """1-based section index."""

    is_rollback_section: bool = False
    mode: str = "toon"
    """'toon' — nodes were compressed | 'text' — raw block text preserved (prose sections)"""

    nodes: List[TOONNode] = field(default_factory=list)

    raw_text: str = ""
    """Populated when mode='text': the section's raw block text for LLM."""


@dataclass
class TOONDocument:
    """
    The complete Tree of Outlined Nodes for a MOP document.

    Produced by: TOONBuilder.build()
    Consumed by: TOONRenderer (→ compact text) → SuperPromptRunner (→ LLM)
    """

    title: str
    source_file: str
    source_format: str
    detected_structure: str

    sections: List[TOONSection]

    # Token estimates
    estimated_raw_tokens: int = 0
    """Estimated tokens in the original document full_text."""

    estimated_toon_tokens: int = 0
    """Estimated tokens in the rendered TOON text."""

    compression_ratio: float = 0.0
    """1 - (toon_tokens / raw_tokens). Higher = more savings."""

    # Usability
    toon_usable: bool = True
    """
    True when it is safe to send the TOON to the LLM instead of raw text.
    False for pure-prose or unknown-structure documents.
    """

    fallback_reason: str = ""
    """Why toon_usable=False, if applicable."""

    all_commands: List[str] = field(default_factory=list)
    """All CLI commands found across the entire document (for guardrail hints)."""
