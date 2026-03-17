"""
Document Loader — entry point for the ingestion layer.

Routes the source file to the appropriate format-specific parser,
then runs auto-detection of MOP structure through the normalizer chain.
"""

from __future__ import annotations

import os
from pathlib import Path

from models.canonical import ParsedDocument
from ingestion.normalizer import detect_structure


def load(file_path: str) -> ParsedDocument:
    """
    Load any supported document and return a ParsedDocument.

    Supported formats: .pdf, .docx, .txt, .text

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

    ext = path.suffix.lower()

    if ext == ".pdf":
        from ingestion.pdf_parser import parse as _parse
    elif ext == ".docx":
        from ingestion.docx_parser import parse as _parse
    elif ext in (".txt", ".text", ".md"):
        from ingestion.txt_parser import parse as _parse
    else:
        raise ValueError(
            f"Unsupported file format '{ext}'. "
            "Supported formats: .pdf, .docx, .txt, .text, .md"
        )

    doc = _parse(str(path))
    doc.detected_structure = detect_structure(doc.blocks)
    return doc
