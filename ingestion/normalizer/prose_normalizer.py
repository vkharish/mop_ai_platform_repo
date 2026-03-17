"""
Prose Normalizer — handles free-form paragraph-based MOPs.

Some MOPs are written as flowing text with embedded CLI commands:
  "To verify the BGP session, connect to PE1 and run 'show ip bgp summary'.
   Ensure all neighbors show state 'Established'. If not, check the MTU
   settings by running 'show interface gi0/0'."

Strategy: Split paragraphs into sentences, preserving inline commands.
The LLM will handle the semantic interpretation.
"""

from __future__ import annotations

import re
from typing import List

from models.canonical import DocumentBlock
from ingestion.normalizer.base_normalizer import BaseNormalizer


# Rough sentence boundary pattern
# Note: Python 3.13+ disallows variable-width lookbehinds; use simple boundary
_SENTENCE_RE = re.compile(r"\.\s+(?=[A-Z])")


class ProseNormalizer(BaseNormalizer):

    @classmethod
    def can_handle(cls, blocks: List[DocumentBlock]) -> bool:
        para_count = sum(1 for b in blocks if b.block_type == "paragraph")
        total = len(blocks) or 1
        return (para_count / total) > 0.6

    @classmethod
    def extract_steps(cls, blocks: List[DocumentBlock]) -> List[str]:
        """
        For prose MOPs, group paragraphs by their section heading and
        pass them as-is to the LLM. The LLM will extract steps.

        We do minimal pre-processing:
        - Strip boilerplate (page numbers, headers/footers)
        - Preserve inline code / command patterns
        """
        steps: List[str] = []
        current_section = ""
        buffer: List[str] = []

        def flush_buffer():
            if buffer:
                text = " ".join(buffer).strip()
                if text:
                    prefix = f"[{current_section}] " if current_section else ""
                    steps.append(f"{prefix}{text}")
                buffer.clear()

        for block in blocks:
            if block.block_type == "heading":
                flush_buffer()
                current_section = block.content
                continue

            if block.block_type in ("paragraph", "code_block"):
                text = block.content.strip()
                if _is_boilerplate(text):
                    continue
                buffer.append(text)

        flush_buffer()
        return steps


def _is_boilerplate(text: str) -> bool:
    """Filter out page numbers, headers, footers, and empty lines."""
    if not text:
        return True
    if re.match(r"^\s*Page\s+\d+\s*(of\s+\d+)?\s*$", text, re.IGNORECASE):
        return True
    if re.match(r"^\s*\d+\s*$", text):  # lone page number
        return True
    if len(text) < 3:
        return True
    return False
