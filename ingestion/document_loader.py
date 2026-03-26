"""
Document Loader — entry point for the ingestion layer.

Routes the source file to the appropriate format-specific parser,
then runs auto-detection of MOP structure through the normalizer chain.

Format detection uses magic bytes first (content-based), falling back to
file extension — this handles files with wrong/missing extensions.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from models.canonical import ParsedDocument
from ingestion.normalizer import detect_structure

# Magic byte signatures
_PDF_MAGIC = b"%PDF"
_ZIP_MAGIC = b"PK\x03\x04"  # DOCX/XLSX/PPTX are all ZIP-based


def load(file_path: str) -> ParsedDocument:
    """
    Load any supported document and return a ParsedDocument.

    Supported formats: .pdf, .docx, .txt, .text, .md

    Format is detected from file content (magic bytes) first, then
    falls back to file extension so renamed/extension-less files still work.

    Args:
        file_path: Absolute or relative path to the MOP document.

    Returns:
        ParsedDocument with typed blocks, full text, and detected structure.

    Raises:
        ValueError: If the file format is not supported.
        FileNotFoundError: If the file does not exist.
    """
    path = Path(file_path).resolve()

    if not path.exists():
        raise FileNotFoundError(f"Document not found: {path}")

    fmt = _detect_format(path)

    if fmt == "pdf":
        from ingestion.pdf_parser import parse as _parse
    elif fmt == "docx":
        from ingestion.docx_parser import parse as _parse
    elif fmt == "txt":
        from ingestion.txt_parser import parse as _parse
    else:
        raise ValueError(
            f"Unsupported file format (detected: '{fmt}', extension: '{path.suffix}'). "
            "Supported formats: .pdf, .docx, .txt, .text, .md"
        )

    doc = _parse(str(path))
    doc.detected_structure = detect_structure(doc.blocks)
    return doc


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def _detect_format(path: Path) -> str:
    """
    Detect document format from magic bytes, falling back to extension.

    Returns one of: 'pdf', 'docx', 'txt', or 'unknown'.
    """
    try:
        with open(path, "rb") as fh:
            header = fh.read(8)
    except OSError:
        return _format_from_extension(path)

    # PDF: starts with %PDF
    if header[:4] == _PDF_MAGIC:
        return "pdf"

    # DOCX: ZIP-based — check for word/document.xml inside the archive
    if header[:4] == _ZIP_MAGIC:
        try:
            with zipfile.ZipFile(path, "r") as zf:
                names = zf.namelist()
            if any(n.startswith("word/") for n in names):
                return "docx"
        except zipfile.BadZipFile:
            pass
        # ZIP but not DOCX (could be XLSX etc.) — fall through to extension
        return _format_from_extension(path)

    # No binary magic match — try extension first
    ext_fmt = _format_from_extension(path)
    if ext_fmt != "unknown":
        return ext_fmt

    # Last resort: sniff content for valid UTF-8 text (no null bytes).
    # Only do this when the extension is absent or clearly not a known binary
    # format (e.g. .xlsx, .ppt, .bin are excluded so they still raise ValueError).
    _KNOWN_BINARY_EXTS = {
        ".xlsx", ".xls", ".ppt", ".pptx", ".odt", ".ods",
        ".zip", ".gz", ".tar", ".bin", ".exe", ".dll",
    }
    if path.suffix.lower() not in _KNOWN_BINARY_EXTS:
        try:
            with open(path, "rb") as fh:
                sample = fh.read(512)
            if b"\x00" not in sample:
                sample.decode("utf-8")
                return "txt"
        except (OSError, UnicodeDecodeError):
            pass

    return "unknown"


def _format_from_extension(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext == ".docx":
        return "docx"
    if ext in (".txt", ".text", ".md"):
        return "txt"
    return "unknown"
