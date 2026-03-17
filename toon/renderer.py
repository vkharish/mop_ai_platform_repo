"""
TOON Renderer — converts a TOONDocument into the compact text fed to the LLM.

Output format example:
  SECTION: Pre-checks
  [s1.1] Verify BGP state | CMD: show ip bgp summary | EXPECT: Established
  [s1.2] Check OSPF adj | CMD: show ip ospf neighbor
  SECTION: Rollback [ROLLBACK]
  [s3.1] Remove BGP config | CMD: no router bgp 65001

Rules:
  - CLI commands are verbatim, joined with ▸ for multi-command sequences.
  - Rollback sections tagged [ROLLBACK].
  - Prose sections (mode='text') included as-is under their heading.
  - Compact: no blank lines between nodes in the same section.
"""

from __future__ import annotations

from typing import Optional

from toon.models import TOONDocument, TOONSection, TOONNode


class TOONRenderer:
    """
    Renders a TOONDocument to a compact UTF-8 string for LLM consumption.

    Usage:
        text = TOONRenderer.render(toon_doc)
        # Pass text to the LLM prompt builder
    """

    CMD_SEP = " \u25b8 "   # ▸
    SECTION_TAG = "SECTION"
    ROLLBACK_TAG = "[ROLLBACK]"

    @classmethod
    def render(cls, doc: TOONDocument) -> str:
        """
        Convert a TOONDocument to a compact text string.

        For toon_usable=False documents this returns an empty string
        (caller should use doc's raw full_text instead).
        """
        if not doc.toon_usable or not doc.sections:
            return ""

        lines: list[str] = []

        # Document title header
        lines.append(f"# {doc.title}")
        lines.append(f"# Source: {doc.source_file}  Structure: {doc.detected_structure}")
        lines.append(
            f"# Compression: {doc.compression_ratio:.1%}  "
            f"({doc.estimated_raw_tokens:,} raw → {doc.estimated_toon_tokens:,} toon tokens)"
        )
        lines.append("")

        for section in doc.sections:
            lines.extend(cls._render_section(section))

        return "\n".join(lines)

    @classmethod
    def render_section_only(cls, section: TOONSection) -> str:
        """Render a single TOONSection (useful for per-chunk LLM calls)."""
        return "\n".join(cls._render_section(section))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @classmethod
    def _render_section(cls, section: TOONSection) -> list[str]:
        lines: list[str] = []

        # Section heading line
        rollback_suffix = f"  {cls.ROLLBACK_TAG}" if section.is_rollback_section else ""
        lines.append(f"{cls.SECTION_TAG}: {section.heading}{rollback_suffix}")

        if section.mode == "text":
            # Prose section — emit raw text directly
            if section.raw_text.strip():
                # Indent slightly to visually separate from headings
                for raw_line in section.raw_text.splitlines():
                    lines.append(f"  {raw_line}" if raw_line.strip() else "")
        else:
            # TOON section — emit compact node lines
            for node in section.nodes:
                rendered = cls._render_node(node)
                if rendered:
                    lines.append(rendered)

        return lines

    @classmethod
    def _render_node(cls, node: TOONNode) -> Optional[str]:
        """Render a single TOONNode to a one-line string."""
        if not node.description and not node.commands:
            return None

        parts: list[str] = []

        # ID + description
        parts.append(f"[{node.node_id}] {node.description}")

        # CLI commands (verbatim, joined with ▸)
        if node.commands:
            cmd_str = cls.CMD_SEP.join(node.commands)
            parts.append(f"CMD: {cmd_str}")

        # Expected output
        if node.expected_output:
            parts.append(f"EXPECT: {node.expected_output}")

        # Rollback flag on individual node (when section isn't already tagged)
        if node.is_rollback and not node.section.lower().startswith("rollback"):
            parts.append("[ROLLBACK]")

        return " | ".join(parts)
