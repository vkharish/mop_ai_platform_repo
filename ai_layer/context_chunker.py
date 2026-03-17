"""
Context Chunker — splits large MOP documents into LLM-sized chunks.

Strategy: section-based chunking with greedy bin packing.

  1. Identify natural section boundaries from HEADING blocks.
  2. Estimate tokens per section (chars / CHARS_PER_TOKEN).
  3. Greedily pack consecutive sections into chunks without exceeding
     MAX_TOKENS_PER_CHUNK.
  4. If a single section exceeds the budget (very long section), split
     it into sub-chunks by blocks, keeping the heading in every sub-chunk
     as context.

Each chunk carries enough context for the LLM to understand its position:
  - Document title
  - Chunk index and total count
  - Section headings included in this chunk
  - Pre-detected CLI hints filtered to this chunk's text

Why 80k tokens per chunk:
  claude-sonnet-4-6 has a 200k token context window.
  Budget breakdown per chunk:
    ~2k   system prompt
    ~5k   instructions + JSON schema
    ~3k   CLI hints
    ~8k   output (MAX_TOKENS)
    ─────
    ~18k  overhead
    80k   document content
    ─────
    ~98k  total (well within 200k limit, leaves headroom)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from models.canonical import DocumentBlock, ParsedDocument


# Conservative: 3.5 chars per token for CLI-heavy technical English
CHARS_PER_TOKEN: float = 3.5

# Max tokens allocated for document content per chunk
MAX_TOKENS_PER_CHUNK: int = 80_000


@dataclass
class DocumentChunk:
    """A single LLM-sized slice of a ParsedDocument."""

    chunk_index: int
    """0-based index of this chunk."""

    total_chunks: int
    """Total number of chunks this document was split into. Set after all chunks are created."""

    section_headings: List[str]
    """All heading texts included in this chunk (for LLM context)."""

    blocks: List[DocumentBlock]
    """The blocks in this chunk."""

    text: str
    """Reconstructed text for this chunk, ready to include in the LLM prompt."""

    estimated_tokens: int
    """Approximate token count for this chunk's text."""

    pre_detected_commands: List[str] = field(default_factory=list)
    """Pre-LLM grammar-engine commands that appear in this chunk's text."""


class ContextChunker:
    """
    Splits a ParsedDocument into LLM-sized DocumentChunks.

    Usage:
        chunker = ContextChunker()
        if chunker.needs_chunking(doc):
            chunks = chunker.chunk(doc, pre_detected_commands)
        else:
            # single chunk — use doc.full_text directly
    """

    def __init__(
        self,
        max_tokens_per_chunk: int = MAX_TOKENS_PER_CHUNK,
        chars_per_token: float = CHARS_PER_TOKEN,
    ):
        self._max_tokens = max_tokens_per_chunk
        self._chars_per_token = chars_per_token

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def needs_chunking(self, doc: ParsedDocument) -> bool:
        """Return True if the document is too large for a single LLM call."""
        return self._estimate_tokens(doc.full_text) > self._max_tokens

    def chunk(
        self,
        doc: ParsedDocument,
        pre_detected_commands: Optional[List[str]] = None,
    ) -> List[DocumentChunk]:
        """
        Split a ParsedDocument into DocumentChunks.

        Args:
            doc:                    The parsed document to split.
            pre_detected_commands:  Grammar-engine CLI commands; each chunk
                                    receives the subset that appears in its text.

        Returns:
            List of DocumentChunk objects (at least 1 even for small docs).
            All chunks have total_chunks set correctly.
        """
        sections = self._group_into_sections(doc.blocks)
        raw_chunks = self._pack_sections(sections)
        chunks = self._build_chunk_objects(raw_chunks, doc, pre_detected_commands or [])
        # Patch total_chunks now that we know the final count
        for c in chunks:
            c.total_chunks = len(chunks)
        return chunks

    def estimate_tokens(self, text: str) -> int:
        """Public token estimator (used by pipeline for logging)."""
        return self._estimate_tokens(text)

    # ------------------------------------------------------------------
    # Section grouping
    # ------------------------------------------------------------------

    def _group_into_sections(
        self, blocks: List[DocumentBlock]
    ) -> List[dict]:
        """
        Group blocks into sections keyed by their preceding heading.

        Returns a list of dicts:
          {"heading": str, "blocks": List[DocumentBlock], "tokens": int}
        """
        sections = []
        current_heading = "_preamble"
        current_blocks: List[DocumentBlock] = []

        for block in blocks:
            if block.block_type == "heading" and block.level <= 2:
                # Flush current section
                if current_blocks:
                    sections.append(self._make_section(current_heading, current_blocks))
                current_heading = block.content
                current_blocks = [block]
            else:
                current_blocks.append(block)

        # Flush last section
        if current_blocks:
            sections.append(self._make_section(current_heading, current_blocks))

        return sections

    def _make_section(self, heading: str, blocks: List[DocumentBlock]) -> dict:
        text = self._blocks_to_text(blocks)
        return {
            "heading": heading,
            "blocks": blocks,
            "text": text,
            "tokens": self._estimate_tokens(text),
        }

    # ------------------------------------------------------------------
    # Bin packing
    # ------------------------------------------------------------------

    def _pack_sections(self, sections: List[dict]) -> List[List[dict]]:
        """
        Greedily pack sections into chunks without exceeding MAX_TOKENS_PER_CHUNK.

        If a single section exceeds the budget, it is split into sub-chunks
        by blocks before packing.
        """
        # Expand any oversized sections first
        expanded: List[dict] = []
        for section in sections:
            if section["tokens"] > self._max_tokens:
                expanded.extend(self._split_oversized_section(section))
            else:
                expanded.append(section)

        # Greedy packing
        chunks: List[List[dict]] = []
        current_chunk: List[dict] = []
        current_tokens = 0

        for section in expanded:
            if current_tokens + section["tokens"] > self._max_tokens and current_chunk:
                chunks.append(current_chunk)
                current_chunk = [section]
                current_tokens = section["tokens"]
            else:
                current_chunk.append(section)
                current_tokens += section["tokens"]

        if current_chunk:
            chunks.append(current_chunk)

        return chunks if chunks else [[]]

    def _split_oversized_section(self, section: dict) -> List[dict]:
        """
        Split a section that exceeds the token budget into sub-chunks by blocks.
        Each sub-chunk keeps the heading block for context.
        """
        heading_block = next(
            (b for b in section["blocks"] if b.block_type == "heading"),
            None,
        )
        heading_text = section["heading"]
        sub_chunks = []
        sub_blocks: List[DocumentBlock] = []
        sub_tokens = 0
        sub_index = 0

        for block in section["blocks"]:
            if block.block_type == "heading" and block.level <= 2:
                continue  # heading is prepended to each sub-chunk

            block_tokens = self._estimate_tokens(block.content)

            if sub_tokens + block_tokens > self._max_tokens and sub_blocks:
                sub_chunks.append(self._make_sub_section(
                    heading_text, sub_index, heading_block, sub_blocks
                ))
                sub_blocks = [block]
                sub_tokens = block_tokens
                sub_index += 1
            else:
                sub_blocks.append(block)
                sub_tokens += block_tokens

        if sub_blocks:
            sub_chunks.append(self._make_sub_section(
                heading_text, sub_index, heading_block, sub_blocks
            ))

        return sub_chunks

    def _make_sub_section(
        self,
        heading_text: str,
        sub_index: int,
        heading_block: Optional[DocumentBlock],
        blocks: List[DocumentBlock],
    ) -> dict:
        # Prepend heading block to each sub-chunk for context
        all_blocks = ([heading_block] if heading_block else []) + blocks
        text = self._blocks_to_text(all_blocks)
        label = f"{heading_text} (continued {sub_index + 1})" if sub_index > 0 else heading_text
        return {
            "heading": label,
            "blocks": all_blocks,
            "text": text,
            "tokens": self._estimate_tokens(text),
        }

    # ------------------------------------------------------------------
    # Chunk object construction
    # ------------------------------------------------------------------

    def _build_chunk_objects(
        self,
        raw_chunks: List[List[dict]],
        doc: ParsedDocument,
        all_pre_commands: List[str],
    ) -> List[DocumentChunk]:
        chunks = []
        for idx, section_group in enumerate(raw_chunks):
            all_blocks = [b for s in section_group for b in s["blocks"]]
            headings = [s["heading"] for s in section_group if s["heading"] != "_preamble"]
            text = "\n\n".join(s["text"] for s in section_group)
            tokens = self._estimate_tokens(text)

            # Filter pre-detected commands to only those appearing in this chunk
            chunk_commands = [
                cmd for cmd in all_pre_commands
                if cmd.lower() in text.lower()
            ]

            chunks.append(DocumentChunk(
                chunk_index=idx,
                total_chunks=0,  # patched after all chunks are built
                section_headings=headings,
                blocks=all_blocks,
                text=text,
                estimated_tokens=tokens,
                pre_detected_commands=chunk_commands,
            ))

        return chunks

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _estimate_tokens(self, text: str) -> int:
        return max(1, int(len(text) / self._chars_per_token))

    @staticmethod
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
            elif b.block_type == "code_block":
                parts.append(f"    {b.content}")
            else:
                parts.append(b.content)
        return "\n".join(p for p in parts if p.strip())
