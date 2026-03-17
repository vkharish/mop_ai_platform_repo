"""
Table Normalizer — handles table-based MOPs.

Common table formats in MOPs:
  | Step | Action                          | Expected Result        | Rollback |
  | 1    | show ip bgp summary             | All neighbors up       |          |
  | 2    | configure interface gi0/0       | Interface comes up     | no shut  |

Or 2-column:
  | Step | Description                                     |
  | 1    | Enable BGP on PE1, verify with show ip bgp sum  |
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from models.canonical import DocumentBlock
from ingestion.normalizer.base_normalizer import BaseNormalizer


# Column header synonyms for semantic mapping
_COLUMN_SYNONYMS: Dict[str, List[str]] = {
    "step":     ["step", "no", "#", "seq", "sequence", "id", "num"],
    "action":   ["action", "description", "task", "procedure", "command", "activity", "what to do"],
    "expected": ["expected", "result", "expected result", "verification", "verify", "output", "expected output"],
    "rollback": ["rollback", "backout", "undo", "revert", "recovery"],
    "notes":    ["notes", "note", "comment", "remarks", "remark"],
}


class TableNormalizer(BaseNormalizer):

    @classmethod
    def can_handle(cls, blocks: List[DocumentBlock]) -> bool:
        table_count = sum(1 for b in blocks if b.block_type == "table_row")
        total = len(blocks) or 1
        return (table_count / total) > 0.3

    @classmethod
    def extract_steps(cls, blocks: List[DocumentBlock]) -> List[str]:
        """
        Convert table rows into step strings, using semantic column detection
        to handle any column ordering or naming convention.
        """
        table_rows = [b for b in blocks if b.block_type == "table_row"]
        if not table_rows:
            return []

        header_row, data_rows = cls._split_header(table_rows)
        col_map = cls._detect_columns(header_row)

        steps: List[str] = []
        for row in data_rows:
            cells = [c.strip() for c in row.content.split(" | ")]
            step_text = cls._row_to_step(cells, col_map, header_row)
            if step_text.strip():
                steps.append(step_text)

        return steps

    @classmethod
    def _split_header(cls, rows: List[DocumentBlock]) -> Tuple[Optional[List[str]], List[DocumentBlock]]:
        """
        Split header row (row_index=0) from data rows.
        If no explicit header row, treat all rows as data.
        """
        header_cells = None
        data_rows = []

        for row in rows:
            if row.row_index == 0:
                header_cells = [c.strip().lower() for c in row.content.split(" | ")]
            else:
                data_rows.append(row)

        # If all rows have row_index == -1 (no indexing), use first row as header
        if header_cells is None and rows:
            first = rows[0]
            header_cells = [c.strip().lower() for c in first.content.split(" | ")]
            data_rows = rows[1:]

        return header_cells, data_rows

    @classmethod
    def _detect_columns(cls, header: Optional[List[str]]) -> Dict[str, int]:
        """
        Map semantic column names to their column indices.
        """
        if not header:
            return {}

        col_map: Dict[str, int] = {}
        for idx, cell in enumerate(header):
            for semantic, synonyms in _COLUMN_SYNONYMS.items():
                if any(s in cell for s in synonyms):
                    if semantic not in col_map:
                        col_map[semantic] = idx
                        break

        return col_map

    @classmethod
    def _row_to_step(
        cls,
        cells: List[str],
        col_map: Dict[str, int],
        header: Optional[List[str]],
    ) -> str:
        """Convert a table row into a step text string."""

        def get(semantic: str) -> str:
            idx = col_map.get(semantic)
            if idx is not None and idx < len(cells):
                return cells[idx].strip()
            return ""

        step_num = get("step")
        action = get("action")
        expected = get("expected")
        rollback = get("rollback")
        notes = get("notes")

        # Fallback: if no col_map, join all non-empty cells
        if not col_map:
            return " — ".join(c for c in cells if c)

        parts = []
        if step_num:
            parts.append(f"Step {step_num}:")
        if action:
            parts.append(action)
        if expected:
            parts.append(f"Expected: {expected}")
        if rollback:
            parts.append(f"Rollback: {rollback}")
        if notes:
            parts.append(f"Notes: {notes}")

        return " | ".join(parts)
