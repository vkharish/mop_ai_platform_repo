"""
List Normalizer — handles numbered and bulleted list MOPs.

These are the most common MOP formats:
  1. Enable maintenance mode
  2. Verify BGP neighbors
     2.1 Run: show ip bgp summary
     2.2 Confirm all neighbors are Established

Or bullet-point style:
  - Enable maintenance mode
  - Verify BGP neighbors
    - Run: show ip bgp summary
"""

from __future__ import annotations

import re
from typing import List

from models.canonical import DocumentBlock
from ingestion.normalizer.base_normalizer import BaseNormalizer


class ListNormalizer(BaseNormalizer):

    @classmethod
    def can_handle(cls, blocks: List[DocumentBlock]) -> bool:
        list_count = sum(1 for b in blocks if b.block_type == "list_item")
        total = len(blocks) or 1
        return (list_count / total) > 0.4

    @classmethod
    def extract_steps(cls, blocks: List[DocumentBlock]) -> List[str]:
        """
        Flatten list items into step strings, preserving parent context
        by prepending the current section heading.
        """
        steps: List[str] = []
        current_section = ""
        current_parent = ""

        for block in blocks:
            if block.block_type == "heading":
                current_section = block.content
                current_parent = ""
                continue

            if block.block_type == "list_item":
                if block.level == 1:
                    # Top-level step
                    current_parent = block.content
                    prefix = f"[{current_section}] " if current_section else ""
                    steps.append(f"{prefix}{block.content}")
                else:
                    # Sub-step — include parent context
                    prefix = f"[{current_section}] " if current_section else ""
                    parent_prefix = f"(sub-step of: {current_parent}) " if current_parent else ""
                    steps.append(f"{prefix}{parent_prefix}{block.content}")

            elif block.block_type in ("paragraph", "code_block"):
                # Paragraphs between list items often contain CLI commands
                # Attach to the most recent step
                if steps:
                    steps[-1] = steps[-1] + f"\n  {block.content}"
                else:
                    steps.append(block.content)

        return steps

    @classmethod
    def is_numbered(cls, blocks: List[DocumentBlock]) -> bool:
        """
        Heuristic to distinguish numbered vs bulleted list MOPs.
        Checks if the source document had step numbering cues.
        """
        numbered_hints = 0
        list_count = 0
        for block in blocks:
            if block.block_type == "list_item":
                list_count += 1
                # Check metadata if the parser stored numbering info
                if block.metadata.get("numbered"):
                    numbered_hints += 1
        # If no metadata hints, check paragraph blocks for numbering patterns
        if numbered_hints == 0:
            for block in blocks:
                if re.match(r"^\d+\.", block.content):
                    numbered_hints += 1

        return numbered_hints > (list_count * 0.3)
