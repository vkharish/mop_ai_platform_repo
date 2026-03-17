"""
TOON Prompt Builder

Constructs the system prompt and user prompt when the document has been
pre-processed into TOON (Tree of Outlined Nodes) format.

The TOON-flavoured prompt is shorter and more structured than the raw-text
super prompt, because the document content has already been compressed to
compact node lines.  This yields both token savings and more consistent LLM
output (the structure is explicit, not inferred).
"""

from __future__ import annotations

from typing import List, Optional

from toon.models import TOONDocument


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

TOON_SYSTEM_PROMPT = """\
You are a senior network operations engineer and test automation expert.
Your task is to convert a pre-structured MOP (Method of Procedure) document,
already formatted as a compact Tree of Outlined Nodes (TOON), into a
structured JSON test-case artifact.

Each TOON node represents one actionable step.  Node IDs like [s2.3] encode
section and step position.  CLI commands follow 'CMD:' and expected output
follows 'EXPECT:'.  Sections tagged [ROLLBACK] are backout steps.

You are vendor-agnostic: detect Cisco, Juniper, Nokia, Arista, F5, Huawei,
Ericsson, Palo Alto, Check Point, or any other CLI style correctly.

You always respond with valid JSON only — no markdown, no explanation, no preamble.\
"""


# ---------------------------------------------------------------------------
# Main TOON prompt template
# ---------------------------------------------------------------------------

_TOON_PROMPT_TEMPLATE = """\
Convert the following TOON-formatted MOP document into the JSON schema below.
Each TOON node ([sX.Y]) maps to exactly one step in the output.

DOCUMENT TITLE: {title}
DETECTED STRUCTURE: {structure}
COMPRESSION RATIO: {compression_ratio:.1%}  ({raw_tokens:,} raw → {toon_tokens:,} TOON tokens)
{cli_hint_section}

EXTRACTION RULES:
1. One TOON node → one JSON step.  Do NOT merge or split nodes.
2. Node ID [sX.Y] → use as a hint for section ordering (not required in output).
3. Text after 'CMD:' is a CLI command (or ▸-separated command sequence). \
Preserve it EXACTLY.
4. Text after 'EXPECT:' is expected_output.
5. Sections tagged [ROLLBACK] → set is_rollback: true for ALL steps in them.
6. Classify step_type: action | verification | rollback | config | info
7. Classify action_type: execute | verify | configure | rollback | observe
8. For each CLI command detect: vendor, protocol, mode.
   vendor:   cisco | juniper | nokia | arista | f5 | huawei | ericsson | palo_alto | checkpoint | generic
   protocol: bgp | ospf | isis | mpls | interface | vlan | system | firewall | lb | null
   mode:     exec | config | null
9. Infer section from the 'SECTION:' heading above the node.
10. Add meaningful tags (vendor, protocol, step-type keywords).

OUTPUT JSON SCHEMA (respond with ONLY this JSON, no other text):
{{
  "document_title": "string",
  "steps": [
    {{
      "sequence": 1,
      "step_type": "action | verification | rollback | config | info",
      "action_type": "execute | verify | configure | rollback | observe",
      "description": "clear human-readable description",
      "raw_text": "the node description text (from TOON, before any CMD/EXPECT)",
      "commands": [
        {{
          "raw": "exact CLI command as in TOON CMD field",
          "vendor": "cisco | juniper | nokia | arista | f5 | huawei | ericsson | palo_alto | checkpoint | generic",
          "protocol": "bgp | ospf | isis | mpls | interface | vlan | system | firewall | lb | null",
          "mode": "exec | config | null",
          "confidence": 0.95
        }}
      ],
      "expected_output": "from TOON EXPECT field, or null",
      "section": "from the SECTION: line above this node",
      "subsection": null,
      "is_rollback": false,
      "tags": ["tag1", "tag2"]
    }}
  ]
}}

---
TOON DOCUMENT:
{toon_text}
---
"""


# ---------------------------------------------------------------------------
# Chunked TOON prompt — when even the TOON exceeds 80k tokens
# ---------------------------------------------------------------------------

_TOON_CHUNK_PROMPT_TEMPLATE = """\
Convert the following SECTION of a TOON-formatted MOP document into JSON steps.
This is chunk {chunk_index} of {total_chunks}.

DOCUMENT TITLE: {title}
SECTIONS IN THIS CHUNK: {section_headings}
{cli_hint_section}

IMPORTANT CHUNKING INSTRUCTIONS:
- Extract ONLY the steps from the TOON content below.
- Use sequence numbers starting from {sequence_start}.
- Sections tagged [ROLLBACK] → is_rollback: true for all steps.

EXTRACTION RULES:
1. One TOON node → one JSON step.
2. Text after 'CMD:' is a verbatim CLI command (▸ separates multi-command sequences).
3. Text after 'EXPECT:' is expected_output.
4. Classify step_type, action_type, vendor, protocol, mode as described.
5. Set section from the SECTION: heading.

OUTPUT JSON SCHEMA (respond with ONLY this JSON):
{{
  "document_title": "string",
  "steps": [
    {{
      "sequence": {sequence_start},
      "step_type": "action | verification | rollback | config | info",
      "action_type": "execute | verify | configure | rollback | observe",
      "description": "human-readable description",
      "raw_text": "node description text",
      "commands": [
        {{
          "raw": "exact CLI command",
          "vendor": "cisco | juniper | nokia | arista | f5 | huawei | ericsson | palo_alto | checkpoint | generic",
          "protocol": "bgp | ospf | isis | mpls | interface | vlan | system | firewall | lb | null",
          "mode": "exec | config | null",
          "confidence": 0.95
        }}
      ],
      "expected_output": "from EXPECT field, or null",
      "section": "section heading",
      "subsection": null,
      "is_rollback": false,
      "tags": ["tag1", "tag2"]
    }}
  ]
}}

---
TOON CHUNK:
{toon_text}
---
"""


# ---------------------------------------------------------------------------
# Retry correction messages (same as super_prompt but TOON-flavoured)
# ---------------------------------------------------------------------------

TOON_JSON_CORRECTION_MESSAGE = """\
Your previous response was not valid JSON. This is attempt {attempt} of 3.
Requirements:
- Respond with ONLY the JSON object (starts with '{{', ends with '}}').
- No markdown code fences, no explanatory text.
- Ensure all strings are properly quoted and all brackets are closed.
Please retry now."""

TOON_SCHEMA_CORRECTION_MESSAGE = """\
Your previous response contained valid JSON but did not match the required schema.
This is attempt {attempt} of 3.

Schema violation:
{validation_error}

Please fix the above and retry with ONLY the corrected JSON object."""


# ---------------------------------------------------------------------------
# Builder functions
# ---------------------------------------------------------------------------

def build_toon_prompt(
    toon_doc: TOONDocument,
    pre_detected_commands: Optional[List[str]] = None,
) -> tuple[str, str]:
    """
    Build the (system_prompt, user_prompt) tuple for a full TOON document LLM call.

    Args:
        toon_doc:              TOONDocument produced by TOONBuilder.
        pre_detected_commands: CLI commands detected pre-LLM by grammar engine.

    Returns:
        (system_prompt, user_prompt) tuple.
    """
    from toon.renderer import TOONRenderer

    toon_text = TOONRenderer.render(toon_doc)
    cli_hint = _build_toon_cli_hint(pre_detected_commands)

    user_prompt = _TOON_PROMPT_TEMPLATE.format(
        title=toon_doc.title,
        structure=toon_doc.detected_structure,
        compression_ratio=toon_doc.compression_ratio,
        raw_tokens=toon_doc.estimated_raw_tokens,
        toon_tokens=toon_doc.estimated_toon_tokens,
        cli_hint_section=cli_hint,
        toon_text=toon_text,
    )

    return TOON_SYSTEM_PROMPT, user_prompt


def build_toon_chunk_prompt(
    toon_text: str,
    title: str,
    section_headings: List[str],
    chunk_index: int,
    total_chunks: int,
    sequence_start: int = 1,
    pre_detected_commands: Optional[List[str]] = None,
) -> tuple[str, str]:
    """
    Build the (system_prompt, user_prompt) tuple for a single TOON chunk LLM call.

    Args:
        toon_text:             The rendered TOON text for this chunk.
        title:                 Document title.
        section_headings:      Headings contained in this chunk.
        chunk_index:           0-based chunk index.
        total_chunks:          Total number of chunks.
        sequence_start:        Step sequence number to start from.
        pre_detected_commands: CLI commands in this chunk.

    Returns:
        (system_prompt, user_prompt) tuple.
    """
    cli_hint = _build_toon_cli_hint(pre_detected_commands)
    headings_str = " → ".join(section_headings) if section_headings else "Document"

    user_prompt = _TOON_CHUNK_PROMPT_TEMPLATE.format(
        title=title,
        chunk_index=chunk_index + 1,
        total_chunks=total_chunks,
        section_headings=headings_str,
        cli_hint_section=cli_hint,
        sequence_start=sequence_start,
        toon_text=toon_text,
    )

    return TOON_SYSTEM_PROMPT, user_prompt


def _build_toon_cli_hint(commands: Optional[List[str]]) -> str:
    if not commands:
        return ""
    cmd_list = "\n".join(f"  - {c}" for c in commands[:50])
    return (
        f"\nPRE-DETECTED CLI COMMANDS (ensure all are included):\n{cmd_list}\n"
    )
