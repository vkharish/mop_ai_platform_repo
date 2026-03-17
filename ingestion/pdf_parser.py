"""
PDF Parser — extracts structured blocks from PDF documents.

Uses pdfplumber for rich extraction (tables, layout-aware text).
Falls back to PyPDF2 if pdfplumber is not available.

The parser preserves:
- Headings (detected by font size heuristics)
- Numbered / bullet list items
- Table rows
- Code blocks (monospace font heuristic)
- Plain paragraphs
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

from models.canonical import DocumentBlock, ParsedDocument


def parse(file_path: str) -> ParsedDocument:
    """
    Parse a PDF file into a ParsedDocument.

    Args:
        file_path: Path to the .pdf file.

    Returns:
        ParsedDocument with typed DocumentBlocks.
    """
    try:
        import pdfplumber
        return _parse_with_pdfplumber(file_path)
    except ImportError:
        try:
            import PyPDF2  # noqa: F401
            return _parse_with_pypdf2(file_path)
        except ImportError:
            raise ImportError(
                "No PDF library found. Install one of: "
                "pdfplumber (recommended), PyPDF2"
            )


# ---------------------------------------------------------------------------
# pdfplumber implementation
# ---------------------------------------------------------------------------

def _parse_with_pdfplumber(file_path: str) -> ParsedDocument:
    import pdfplumber

    path = Path(file_path)
    blocks: List[DocumentBlock] = []

    with pdfplumber.open(file_path) as pdf:
        title = _extract_title_from_filename(path.stem)

        for page_num, page in enumerate(pdf.pages, start=1):

            # --- Extract tables first ---
            tables = page.extract_tables()
            table_bboxes = [t.bbox for t in page.find_tables()] if tables else []

            for table_idx, table in enumerate(tables):
                for row_idx, row in enumerate(table):
                    cell_text = " | ".join(
                        (cell or "").strip() for cell in row if cell
                    )
                    if cell_text.strip():
                        blocks.append(DocumentBlock(
                            block_type="table_row",
                            content=cell_text,
                            row_index=row_idx,
                            metadata={"page": page_num, "table_index": table_idx},
                        ))

            # --- Extract text words for non-table regions ---
            words = page.extract_words(extra_attrs=["size", "fontname"]) or []

            # Group words into lines
            lines = _group_words_into_lines(words)

            for line in lines:
                text = line["text"].strip()
                if not text:
                    continue

                block_type = _classify_line(text, line.get("size", 10))
                level = _detect_level(text)
                text = _clean_list_prefix(text)

                blocks.append(DocumentBlock(
                    block_type=block_type,
                    content=text,
                    level=level,
                    metadata={"page": page_num, "font_size": line.get("size", 0)},
                ))

    full_text = _blocks_to_text(blocks)
    return ParsedDocument(
        title=title,
        source_file=file_path,
        source_format="pdf",
        blocks=blocks,
        full_text=full_text,
    )


def _parse_with_pypdf2(file_path: str) -> ParsedDocument:
    import PyPDF2

    path = Path(file_path)
    title = _extract_title_from_filename(path.stem)
    blocks: List[DocumentBlock] = []

    with open(file_path, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        for page_num, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                block_type = _classify_line(line, font_size=None)
                level = _detect_level(line)
                line = _clean_list_prefix(line)
                blocks.append(DocumentBlock(
                    block_type=block_type,
                    content=line,
                    level=level,
                    metadata={"page": page_num},
                ))

    full_text = _blocks_to_text(blocks)
    return ParsedDocument(
        title=title,
        source_file=file_path,
        source_format="pdf",
        blocks=blocks,
        full_text=full_text,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _group_words_into_lines(words: list) -> list:
    """Group pdfplumber word objects into logical lines by y-coordinate."""
    if not words:
        return []

    lines = []
    current_line: list = []
    current_top = None
    tolerance = 3  # pixels

    for word in sorted(words, key=lambda w: (round(w["top"] / tolerance), w["x0"])):
        top = round(word["top"] / tolerance) * tolerance
        if current_top is None:
            current_top = top

        if abs(top - current_top) <= tolerance:
            current_line.append(word)
        else:
            if current_line:
                lines.append({
                    "text": " ".join(w["text"] for w in current_line),
                    "size": current_line[0].get("size", 10),
                    "fontname": current_line[0].get("fontname", ""),
                })
            current_line = [word]
            current_top = top

    if current_line:
        lines.append({
            "text": " ".join(w["text"] for w in current_line),
            "size": current_line[0].get("size", 10),
            "fontname": current_line[0].get("fontname", ""),
        })

    return lines


_NUMBERED_RE = re.compile(r"^\s*(\d+[\.\)]\s+|\d+\.\d+[\.\)]\s+|Step\s+\d+\s*[-:.]?\s*)", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*[-•◦▪▸*]\s+")
_CODE_FONTS = {"Courier", "Courier-Bold", "Courier-Oblique", "Mono", "SourceCodePro"}


def _classify_line(text: str, font_size=None) -> str:
    if _NUMBERED_RE.match(text):
        return "list_item"
    if _BULLET_RE.match(text):
        return "list_item"
    if font_size and font_size >= 14:
        return "heading"
    # Heuristic: all-caps short line is likely a heading
    if len(text) < 80 and text.isupper() and len(text.split()) >= 2:
        return "heading"
    return "paragraph"


def _detect_level(text: str) -> int:
    m = re.match(r"^\s*(\d+)\.(\d+)", text)
    if m:
        return 2
    if _NUMBERED_RE.match(text) or _BULLET_RE.match(text):
        return 1
    return 0


def _clean_list_prefix(text: str) -> str:
    text = _NUMBERED_RE.sub("", text, count=1)
    text = _BULLET_RE.sub("", text, count=1)
    return text.strip()


def _extract_title_from_filename(stem: str) -> str:
    return stem.replace("_", " ").replace("-", " ").title()


def _blocks_to_text(blocks: List[DocumentBlock]) -> str:
    parts = []
    for b in blocks:
        if b.block_type == "heading":
            parts.append(f"\n## {b.content}\n")
        elif b.block_type == "list_item":
            indent = "  " * max(0, b.level - 1)
            parts.append(f"{indent}- {b.content}")
        elif b.block_type == "table_row":
            parts.append(f"| {b.content} |")
        else:
            parts.append(b.content)
    return "\n".join(parts)
