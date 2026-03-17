"""
TOON Builder — converts a ParsedDocument into a TOONDocument.

Pure Python, no LLM, no network calls. Runs in ~50ms even for 200-page docs.

Token savings:
  Raw 200-page MOP : ~400k tokens
  TOON equivalent  : ~30-50k tokens   (85-90% reduction)

Structure-mode routing:
  numbered_list → toon   (structured, safe to compress)
  bulleted_list → toon
  table         → toon
  prose         → text   (LLM must see full prose; TOON would lose context)
  mixed         → per-section: list/table sections → toon, prose sections → text
  unknown       → text   (conservative fallback)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from models.canonical import DocumentBlock, ParsedDocument
from toon.models import TOONDocument, TOONNode, TOONNodeType, TOONSection
from toon.compressor import ProseAnalyzer, TextCompressor

# Minimum significance score for a prose block to become a TOON node
_PROSE_MIN_SCORE = 1

# Rollback section heading pattern
_ROLLBACK_RE = re.compile(
    r"\b(rollback|backout|back.?out|recovery|revert|undo|restore|fallback)\b",
    re.IGNORECASE,
)

# Section-mode decision: structure_type → default mode
_STRUCTURE_MODE: Dict[str, str] = {
    "numbered_list": "toon",
    "bulleted_list": "toon",
    "table":         "toon",
    "prose":         "text",
    "mixed":         "mixed",   # decide per-section
    "unknown":       "text",
}

# Fraction of list/table blocks in a section to qualify it as "toon-safe"
_TOON_SAFE_RATIO = 0.4

# Chars per token (conservative for CLI-heavy text)
_CHARS_PER_TOKEN: float = 3.5


class TOONBuilder:
    """
    Builds a TOONDocument from a ParsedDocument.

    Usage:
        from toon.builder import TOONBuilder
        from grammar_engine.cli_grammar import CLIGrammar

        grammar = CLIGrammar()
        toon_doc = TOONBuilder.build(doc, grammar)
    """

    @classmethod
    def build(cls, doc: ParsedDocument, grammar) -> TOONDocument:
        """
        Build a TOONDocument from a ParsedDocument.

        Args:
            doc:     ParsedDocument from the ingestion layer.
            grammar: CLIGrammar instance for command extraction.

        Returns:
            TOONDocument — may have toon_usable=False for prose docs.
        """
        doc_mode = _STRUCTURE_MODE.get(doc.detected_structure, "text")

        if doc_mode == "text":
            return cls._build_text_fallback(doc)

        sections_raw = cls._group_blocks_by_section(doc.blocks)
        toon_sections: List[TOONSection] = []
        all_commands: List[str] = []
        section_index = 0

        for heading, blocks in sections_raw:
            section_index += 1
            is_rollback = bool(_ROLLBACK_RE.search(heading))

            # Determine mode for this section
            if doc_mode == "mixed":
                mode = cls._section_mode(blocks)
            else:
                mode = "toon"

            if mode == "toon":
                nodes, cmds = cls._process_toon_section(
                    blocks, heading, section_index, is_rollback, grammar
                )
                toon_sections.append(TOONSection(
                    heading=heading,
                    section_index=section_index,
                    is_rollback_section=is_rollback,
                    mode="toon",
                    nodes=nodes,
                ))
                all_commands.extend(cmds)
            else:
                # Prose section — keep raw text for LLM
                raw = "\n".join(b.content for b in blocks if b.content.strip())
                toon_sections.append(TOONSection(
                    heading=heading,
                    section_index=section_index,
                    is_rollback_section=is_rollback,
                    mode="text",
                    raw_text=raw,
                ))

        raw_tokens   = cls._est_tokens(doc.full_text)
        toon_text    = cls._rough_toon_text(toon_sections)
        toon_tokens  = cls._est_tokens(toon_text)
        compression  = max(0.0, 1.0 - (toon_tokens / max(1, raw_tokens)))

        return TOONDocument(
            title=doc.title,
            source_file=doc.source_file,
            source_format=doc.source_format,
            detected_structure=doc.detected_structure,
            sections=toon_sections,
            estimated_raw_tokens=raw_tokens,
            estimated_toon_tokens=toon_tokens,
            compression_ratio=round(compression, 3),
            toon_usable=True,
            all_commands=list(dict.fromkeys(all_commands)),  # dedup, preserve order
        )

    # ------------------------------------------------------------------
    # Section grouping
    # ------------------------------------------------------------------

    @classmethod
    def _group_blocks_by_section(
        cls, blocks: List[DocumentBlock]
    ) -> List[Tuple[str, List[DocumentBlock]]]:
        """
        Group blocks by heading. Returns list of (heading, blocks).
        The "_preamble" section captures blocks before the first heading.
        """
        sections: List[Tuple[str, List[DocumentBlock]]] = []
        current_heading = "_preamble"
        current_blocks: List[DocumentBlock] = []

        for block in blocks:
            if block.block_type == "heading" and block.level <= 2:
                if current_blocks:
                    sections.append((current_heading, current_blocks))
                current_heading = block.content
                current_blocks = []
            else:
                current_blocks.append(block)

        if current_blocks:
            sections.append((current_heading, current_blocks))

        # Drop empty preamble
        return [(h, b) for h, b in sections if b or h != "_preamble"]

    # ------------------------------------------------------------------
    # Section mode detection (for mixed docs)
    # ------------------------------------------------------------------

    @staticmethod
    def _section_mode(blocks: List[DocumentBlock]) -> str:
        """
        Return 'toon' if this section is structured enough to compress,
        'text' if it's primarily prose.
        """
        if not blocks:
            return "text"
        structured = sum(
            1 for b in blocks
            if b.block_type in ("list_item", "table_row", "code_block")
        )
        return "toon" if (structured / len(blocks)) >= _TOON_SAFE_RATIO else "text"

    # ------------------------------------------------------------------
    # TOON section processing
    # ------------------------------------------------------------------

    @classmethod
    def _process_toon_section(
        cls,
        blocks: List[DocumentBlock],
        section_heading: str,
        section_index: int,
        is_rollback: bool,
        grammar,
    ) -> Tuple[List[TOONNode], List[str]]:
        """
        Convert a section's blocks into TOONNodes.
        Returns (nodes, all_commands_found).
        """
        nodes: List[TOONNode] = []
        all_commands: List[str] = []
        step_index = 0

        # For table processing, collect consecutive table rows
        table_buffer: List[DocumentBlock] = []

        def flush_table():
            nonlocal step_index
            if not table_buffer:
                return
            table_nodes, cmds = cls._process_table_rows(
                table_buffer, section_heading, section_index, step_index, is_rollback
            )
            for n in table_nodes:
                step_index += 1
                n.node_id = f"s{section_index}.{step_index}"
            nodes.extend(table_nodes)
            all_commands.extend(cmds)
            table_buffer.clear()

        for block in blocks:
            if block.block_type == "table_row":
                table_buffer.append(block)
                continue
            else:
                flush_table()  # flush any buffered table rows first

            node: Optional[TOONNode] = None

            if block.block_type == "list_item":
                node = cls._process_list_item(block, section_heading, is_rollback, grammar)

            elif block.block_type == "code_block":
                node = cls._process_code_block(block, section_heading, is_rollback, grammar)

            elif block.block_type == "paragraph":
                node = cls._process_paragraph(block, section_heading, is_rollback, grammar)

            if node is not None:
                step_index += 1
                node.node_id = f"s{section_index}.{step_index}"
                nodes.append(node)
                all_commands.extend(node.commands)

        flush_table()  # flush any remaining table rows
        return nodes, all_commands

    # ------------------------------------------------------------------
    # Block-type processors
    # ------------------------------------------------------------------

    @classmethod
    def _process_list_item(
        cls, block: DocumentBlock, section: str, is_rollback: bool, grammar
    ) -> Optional[TOONNode]:
        desc_compressed = TextCompressor.compress_and_truncate(block.content)
        if len(desc_compressed) < 5:
            return None

        cmds = [c.raw for c in grammar.extract_from_text(block.content)]
        expected = ProseAnalyzer.extract_expected(block.content)

        return TOONNode(
            node_type=TOONNodeType.LIST_STEP,
            node_id="",  # assigned by caller
            section=section,
            description=desc_compressed,
            commands=cmds,
            expected_output=expected,
            is_rollback=is_rollback,
            source_block_type="list_item",
        )

    @classmethod
    def _process_code_block(
        cls, block: DocumentBlock, section: str, is_rollback: bool, grammar
    ) -> Optional[TOONNode]:
        # Each line of a code block may be a separate command
        cmds = [c.raw for c in grammar.extract_from_text(block.content)]
        if not cmds:
            return None  # code block with no recognized commands — skip

        desc = f"Execute {len(cmds)} command(s)"
        return TOONNode(
            node_type=TOONNodeType.CODE_STEP,
            node_id="",
            section=section,
            description=desc,
            commands=cmds,
            is_rollback=is_rollback,
            source_block_type="code_block",
        )

    @classmethod
    def _process_paragraph(
        cls, block: DocumentBlock, section: str, is_rollback: bool, grammar
    ) -> Optional[TOONNode]:
        if ProseAnalyzer.score(block.content) < _PROSE_MIN_SCORE:
            return None  # pure boilerplate — skip

        desc = TextCompressor.compress_and_truncate(block.content)
        cmds = [c.raw for c in grammar.extract_from_text(block.content)]
        expected = ProseAnalyzer.extract_expected(block.content)

        return TOONNode(
            node_type=TOONNodeType.PROSE_STEP,
            node_id="",
            section=section,
            description=desc,
            commands=cmds,
            expected_output=expected,
            is_rollback=is_rollback,
            source_block_type="paragraph",
        )

    @classmethod
    def _process_table_rows(
        cls,
        rows: List[DocumentBlock],
        section: str,
        section_index: int,
        start_step: int,
        is_rollback_section: bool,
        grammar=None,
    ) -> Tuple[List[TOONNode], List[str]]:
        """
        Convert buffered table rows into TOONNodes using semantic column detection.
        Handles merged cells by carrying forward the last non-empty cell value.
        """
        from ingestion.normalizer.table_normalizer import TableNormalizer

        if not rows:
            return [], []

        header_cells, data_rows = TableNormalizer._split_header(rows)
        col_map = TableNormalizer._detect_columns(header_cells)

        # For tables with no recognized columns, fall back to positional mapping
        if not col_map and header_cells:
            col_map = {
                "action":   0,
                "expected": 1 if len(header_cells) > 1 else None,
                "rollback": 2 if len(header_cells) > 2 else None,
            }

        nodes: List[TOONNode] = []
        all_cmds: List[str] = []
        prev_cells: Dict[int, str] = {}  # carry-forward for merged cells

        for row in data_rows:
            cells = [c.strip() for c in row.content.split(" | ")]

            # Carry-forward merged cells (empty cell = previous value)
            for idx, cell in enumerate(cells):
                if cell:
                    prev_cells[idx] = cell
                elif idx in prev_cells:
                    cells[idx] = prev_cells[idx]

            def get(semantic: str) -> str:
                idx = col_map.get(semantic)
                if idx is not None and idx < len(cells):
                    return cells[idx].strip()
                return ""

            action_text = get("action") or " | ".join(c for c in cells if c)
            if not action_text.strip():
                continue

            expected = get("expected") or ProseAnalyzer.extract_expected(action_text) or None
            rollback_cell = get("rollback")
            is_rollback = is_rollback_section or bool(rollback_cell)

            desc = TextCompressor.compress_and_truncate(action_text)
            if grammar:
                cmds = [c.raw for c in grammar.extract_from_text(action_text)]
            else:
                cmds = []
            all_cmds.extend(cmds)

            nodes.append(TOONNode(
                node_type=TOONNodeType.TABLE_STEP,
                node_id="",
                section=section,
                description=desc,
                commands=cmds,
                expected_output=expected,
                is_rollback=is_rollback,
                source_block_type="table_row",
                metadata={"rollback_cmd": rollback_cell} if rollback_cell else {},
            ))

        return nodes, all_cmds

    # ------------------------------------------------------------------
    # Text fallback (prose / unknown docs)
    # ------------------------------------------------------------------

    @classmethod
    def _build_text_fallback(cls, doc: ParsedDocument) -> TOONDocument:
        """
        For prose/unknown documents, return a TOONDocument with toon_usable=False.
        The pipeline will use the raw full_text instead.
        """
        return TOONDocument(
            title=doc.title,
            source_file=doc.source_file,
            source_format=doc.source_format,
            detected_structure=doc.detected_structure,
            sections=[],
            estimated_raw_tokens=cls._est_tokens(doc.full_text),
            estimated_toon_tokens=0,
            compression_ratio=0.0,
            toon_usable=False,
            fallback_reason=(
                f"Structure '{doc.detected_structure}' cannot be safely compressed "
                "to TOON — LLM will receive full text."
            ),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _est_tokens(text: str) -> int:
        return max(1, int(len(text) / _CHARS_PER_TOKEN))

    @classmethod
    def _rough_toon_text(cls, sections: List[TOONSection]) -> str:
        """Produce a rough TOON text just for token estimation."""
        lines = []
        for s in sections:
            lines.append(f"SECTION: {s.heading}")
            if s.mode == "text":
                lines.append(s.raw_text[:200])  # sample for estimation
            else:
                for n in s.nodes:
                    line = f"[{n.node_id}] {n.description}"
                    if n.commands:
                        line += " | CMD: " + " ▸ ".join(n.commands)
                    if n.expected_output:
                        line += f" | EXPECT: {n.expected_output}"
                    lines.append(line)
        return "\n".join(lines)
