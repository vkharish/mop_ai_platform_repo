"""
OCR Fallback — scanned PDF support via pdf2image + pytesseract.

This module is a soft dependency: if pdf2image or pytesseract are not
installed the functions log a warning and return None instead of raising.

Install optional deps to enable:
    pip install pdf2image pytesseract Pillow
    # Also install the Tesseract OCR engine (OS-level):
    # macOS:  brew install tesseract
    # Ubuntu: apt-get install tesseract-ocr

Typical usage (called from pdf_parser.py):

    from ingestion.ocr_fallback import is_scanned_pdf, ocr_pdf

    if is_scanned_pdf(parsed_doc):
        ocr_result = ocr_pdf(file_path)
        if ocr_result is not None:
            return ocr_result
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from models.canonical import DocumentBlock, ParsedDocument

logger = logging.getLogger(__name__)

# Threshold: if fewer than this fraction of pages have extractable text,
# treat the PDF as scanned.
_SCANNED_TEXT_THRESHOLD = 0.10
# Minimum characters per page to count as "has text"
_MIN_CHARS_PER_PAGE = 20


def is_scanned_pdf(doc: ParsedDocument, total_pages: Optional[int] = None) -> bool:
    """
    Return True if the ParsedDocument looks like a scanned (image-only) PDF.

    Heuristic: a page "has text" if at least _MIN_CHARS_PER_PAGE characters
    were extracted from it.  If fewer than _SCANNED_TEXT_THRESHOLD of pages
    have text, we classify the PDF as scanned.

    Args:
        doc: Already-parsed document from pdf_parser.parse().
        total_pages: Total page count from the PDF (passed through from the
                     parser so we can compare against blank pages too).
                     If None, derived from block metadata.

    Returns:
        True if the document appears to be scanned / image-based.
    """
    if not doc.blocks:
        return True

    # Gather per-page character counts from block metadata
    page_chars: dict[int, int] = {}
    for block in doc.blocks:
        page = block.metadata.get("page", 1) if block.metadata else 1
        page_chars[page] = page_chars.get(page, 0) + len(block.content)

    if total_pages is None:
        total_pages = max(page_chars.keys(), default=1)

    pages_with_text = sum(1 for chars in page_chars.values() if chars >= _MIN_CHARS_PER_PAGE)
    fraction = pages_with_text / max(total_pages, 1)
    scanned = fraction < _SCANNED_TEXT_THRESHOLD

    if scanned:
        logger.info(
            "PDF appears scanned: %d/%d pages have text (%.0f%% < %.0f%% threshold) — "
            "attempting OCR fallback",
            pages_with_text, total_pages, fraction * 100, _SCANNED_TEXT_THRESHOLD * 100,
        )

    return scanned


def ocr_pdf(file_path: str) -> Optional[ParsedDocument]:
    """
    Run OCR on a PDF file and return a ParsedDocument.

    Requires: pdf2image, pytesseract, Pillow, and the Tesseract binary.
    Returns None (with a warning log) if any dependency is missing.

    Args:
        file_path: Path to the PDF file to OCR.

    Returns:
        ParsedDocument on success, None if OCR deps are unavailable.
    """
    try:
        from pdf2image import convert_from_path  # type: ignore
        import pytesseract  # type: ignore
    except ImportError as exc:
        logger.warning(
            "OCR fallback unavailable — missing dependency: %s. "
            "Install with: pip install pdf2image pytesseract Pillow",
            exc,
        )
        return None

    path = Path(file_path)
    title = path.stem.replace("_", " ").replace("-", " ").title()
    blocks: list[DocumentBlock] = []

    logger.info("Starting OCR on %s", path.name)

    try:
        images = convert_from_path(str(path), dpi=300)
    except Exception as exc:
        logger.error("pdf2image failed to convert %s: %s", path.name, exc)
        return None

    for page_num, image in enumerate(images, start=1):
        try:
            raw_text: str = pytesseract.image_to_string(image, lang="eng")
        except Exception as exc:
            logger.warning("pytesseract failed on page %d of %s: %s", page_num, path.name, exc)
            continue

        for line in raw_text.split("\n"):
            line = line.strip()
            if not line:
                continue

            block_type = _classify_ocr_line(line)
            level = _detect_level(line)
            content = _clean_prefix(line)

            blocks.append(DocumentBlock(
                block_type=block_type,
                content=content,
                level=level,
                metadata={"page": page_num, "ocr": True},
            ))

    logger.info("OCR complete: %d pages → %d blocks", len(images), len(blocks))

    full_text = _blocks_to_text(blocks)
    return ParsedDocument(
        title=title,
        source_file=file_path,
        source_format="pdf",
        blocks=blocks,
        full_text=full_text,
        metadata={"ocr_used": True, "ocr_pages": len(images)},
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

_NUMBERED_RE = re.compile(
    r"^\s*(\d+[\.\)]\s+|\d+\.\d+[\.\)]\s+|Step\s+\d+\s*[-:.]?\s*)", re.IGNORECASE
)
_BULLET_RE = re.compile(r"^\s*[-•◦▪▸*]\s+")
_HEADING_RE = re.compile(r"^[A-Z][A-Z\s\-–:]{5,}$")


def _classify_ocr_line(text: str) -> str:
    if _NUMBERED_RE.match(text):
        return "list_item"
    if _BULLET_RE.match(text):
        return "list_item"
    if _HEADING_RE.match(text) and len(text) < 80:
        return "heading"
    return "paragraph"


def _detect_level(text: str) -> int:
    if re.match(r"^\s*\d+\.\d+", text):
        return 2
    if _NUMBERED_RE.match(text) or _BULLET_RE.match(text):
        return 1
    return 0


def _clean_prefix(text: str) -> str:
    text = _NUMBERED_RE.sub("", text, count=1)
    text = _BULLET_RE.sub("", text, count=1)
    return text.strip()


def _blocks_to_text(blocks: list[DocumentBlock]) -> str:
    parts = []
    for b in blocks:
        if b.block_type == "heading":
            parts.append(f"\n## {b.content}\n")
        elif b.block_type == "list_item":
            indent = "  " * max(0, b.level - 1)
            parts.append(f"{indent}- {b.content}")
        else:
            parts.append(b.content)
    return "\n".join(parts)
