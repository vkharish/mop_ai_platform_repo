"""
MOP Structure Normalizer

Auto-detects the formatting structure of a MOP document from its blocks.
This is informational metadata — the LLM handles all structures uniformly.
The detected structure is passed to the LLM as a hint for better extraction.
"""

from __future__ import annotations

from typing import List

from models.canonical import DocumentBlock
from ingestion.normalizer.list_normalizer import ListNormalizer
from ingestion.normalizer.table_normalizer import TableNormalizer
from ingestion.normalizer.prose_normalizer import ProseNormalizer


def detect_structure(blocks: List[DocumentBlock]) -> str:
    """
    Auto-detect the MOP document structure from its blocks.

    Returns one of:
        numbered_list   — steps are numbered (1. 2. 3. or Step 1, Step 2)
        bulleted_list   — steps are bullet points
        table           — steps are in a table
        prose           — free-form paragraphs
        mixed           — combination of the above
        unknown         — cannot determine
    """
    if not blocks:
        return "unknown"

    counts = {
        "table_row": 0,
        "list_item": 0,
        "paragraph": 0,
        "heading": 0,
        "code_block": 0,
    }

    for b in blocks:
        if b.block_type in counts:
            counts[b.block_type] += 1

    total = sum(counts.values()) or 1

    table_ratio = counts["table_row"] / total
    list_ratio = counts["list_item"] / total
    prose_ratio = counts["paragraph"] / total

    if table_ratio > 0.4:
        return "table"

    if list_ratio > 0.5:
        # Distinguish numbered vs bulleted by looking at normalizer
        if ListNormalizer.is_numbered(blocks):
            return "numbered_list"
        return "bulleted_list"

    if prose_ratio > 0.7:
        return "prose"

    # Mixed: meaningful presence of multiple types
    active_types = sum(1 for k, v in counts.items() if v > 2)
    if active_types >= 2:
        return "mixed"

    return "unknown"


__all__ = ["detect_structure"]
