"""
Plain Text / Markdown Parser — extracts structured blocks from .txt and .md files.

Handles:
- Markdown headings (# Heading)
- Numbered lists
- Bullet lists
- Code fences (``` or indented 4+ spaces)
- Plain paragraphs

Works well for MOPs that are copy-pasted into plain text or authored in Markdown.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

from models.canonical import DocumentBlock, ParsedDocument
from ingestion.pdf_parser import _blocks_to_text


def parse(file_path: str) -> ParsedDocument:
    """
    Parse a plain text or Markdown file into a ParsedDocument.

    Args:
        file_path: Path to the .txt / .md file.

    Returns:
        ParsedDocument with typed DocumentBlocks.
    """
    path = Path(file_path)
    text = path.read_text(encoding="utf-8", errors="replace")

    blocks = _parse_text(text)
    full_text = _blocks_to_text(blocks)
    title = _extract_title(blocks) or _filename_to_title(path.stem)

    return ParsedDocument(
        title=title,
        source_file=file_path,
        source_format="txt",
        blocks=blocks,
        full_text=full_text,
    )


# ---------------------------------------------------------------------------
# Internal parser
# ---------------------------------------------------------------------------

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_NUMBERED_RE = re.compile(r"^\s*(\d+[\.\)]\s+|\d+\.\d+[\.\)]\s+)")
_STEP_RE = re.compile(r"^\s*Step\s+\d+\s*[-:.]?\s*", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*[-•◦▪▸*]\s+")
_CODE_FENCE_RE = re.compile(r"^```")
_INDENTED_CODE_RE = re.compile(r"^    .+")  # 4-space indent


def _parse_text(text: str) -> List[DocumentBlock]:
    blocks: List[DocumentBlock] = []
    lines = text.splitlines()

    in_code_fence = False
    code_buffer: List[str] = []

    for line in lines:

        # --- Code fence toggle ---
        if _CODE_FENCE_RE.match(line):
            if in_code_fence:
                # End of code block
                if code_buffer:
                    blocks.append(DocumentBlock(
                        block_type="code_block",
                        content="\n".join(code_buffer),
                    ))
                    code_buffer = []
                in_code_fence = False
            else:
                in_code_fence = True
            continue

        if in_code_fence:
            code_buffer.append(line)
            continue

        stripped = line.strip()
        if not stripped:
            continue

        # --- Markdown heading ---
        m = _MD_HEADING_RE.match(stripped)
        if m:
            level = len(m.group(1))
            blocks.append(DocumentBlock(
                block_type="heading",
                content=m.group(2).strip(),
                level=level,
            ))
            continue

        # --- 4-space indented code ---
        if _INDENTED_CODE_RE.match(line):
            blocks.append(DocumentBlock(
                block_type="code_block",
                content=stripped,
            ))
            continue

        # --- Numbered list ---
        if _NUMBERED_RE.match(stripped) or _STEP_RE.match(stripped):
            clean = _NUMBERED_RE.sub("", stripped, count=1)
            clean = _STEP_RE.sub("", clean, count=1).strip()
            level = _get_numbered_level(stripped)
            blocks.append(DocumentBlock(
                block_type="list_item",
                content=clean,
                level=level,
            ))
            continue

        # --- Bullet list ---
        if _BULLET_RE.match(stripped):
            clean = _BULLET_RE.sub("", stripped, count=1).strip()
            blocks.append(DocumentBlock(
                block_type="list_item",
                content=clean,
                level=1,
            ))
            continue

        # --- Plain paragraph ---
        blocks.append(DocumentBlock(
            block_type="paragraph",
            content=stripped,
        ))

    return blocks


def _get_numbered_level(text: str) -> int:
    """Detect nesting level from numbering like 1.2.3."""
    m = re.match(r"^\s*(\d+\.)+", text)
    if m:
        return m.group(0).count(".")
    return 1


def _extract_title(blocks: List[DocumentBlock]) -> str | None:
    for b in blocks:
        if b.block_type == "heading" and b.content.strip():
            return b.content.strip()
    return None


def _filename_to_title(stem: str) -> str:
    return stem.replace("_", " ").replace("-", " ").title()
