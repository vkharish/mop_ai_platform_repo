"""
Super Prompt Builder

Constructs the format-agnostic MOP extraction prompt for the LLM.

Design goals:
  - Handles ANY MOP format (numbered list, table, prose, bullet, mixed)
  - Produces strict, schema-validated JSON output
  - Vendor and protocol agnostic (Cisco, Juniper, Nokia, Arista, etc.)
  - Includes the grammar engine's pre-detected CLI commands as a hint
    so the LLM doesn't miss them
"""

from __future__ import annotations

from typing import List, Optional

from ai_layer.context_chunker import DocumentChunk


# ---------------------------------------------------------------------------
# System prompt (sent as the system role)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a senior network operations engineer and test automation expert. \
Your task is to analyze Method of Procedure (MOP) documents and extract \
all actionable steps into a structured JSON format.

You are format-agnostic: MOPs can be written as numbered lists, bullet points, \
tables, free-form prose, or any combination. You must extract ALL steps \
regardless of the document format.

You always respond with valid JSON only — no markdown, no explanation, no preamble.\
"""


# ---------------------------------------------------------------------------
# Main prompt template
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """\
Analyze the following MOP document and extract ALL actionable steps.

DOCUMENT TITLE: {title}
DETECTED STRUCTURE: {structure}
{cli_hint_section}

EXTRACTION RULES:
1. Extract EVERY step — do not skip, summarize, or merge steps.
2. Preserve CLI commands EXACTLY as written in the document (including flags and arguments).
3. If a step has multiple commands, list all of them in the "commands" array.
4. Classify each step:
   - "action"        → operator does something (run a command, configure something)
   - "verification"  → operator checks or validates something
   - "rollback"      → backout / recovery step
   - "config"        → configuration push (bulk config)
   - "info"          → informational note, no operator action needed
5. Classify the action_type:
   - "execute"       → run a command
   - "verify"        → verify output or system state
   - "configure"     → push device configuration
   - "rollback"      → undo or revert a change
   - "observe"       → watch or monitor without active intervention
6. For each CLI command, detect:
   - vendor: cisco | juniper | nokia | arista | generic
   - protocol: bgp | ospf | isis | mpls | interface | vlan | system | null
   - mode: exec | config | null
7. Extract expected_output if the document mentions what the operator should see.
8. Set is_rollback: true for any step in a Rollback, Backout, or Recovery section.
9. Assign section from document headings (e.g., "Pre-checks", "Implementation", "Rollback").
10. Add meaningful tags (e.g., ["bgp", "cisco", "critical", "verification"]).

IMPORTANT — TABLE FORMAT:
If the document uses a table, each row is one step. Use all columns:
Step/No, Action/Description, Expected Result/Verification, Rollback/Backout.

IMPORTANT — PROSE FORMAT:
If the document uses prose paragraphs, each sentence or group of sentences
describing a distinct operation is one step.

IMPORTANT — MIXED FORMAT:
Handle sections differently if they use different formats.

OUTPUT JSON SCHEMA (respond with ONLY this JSON, no other text):
{{
  "document_title": "string",
  "steps": [
    {{
      "sequence": 1,
      "step_type": "action | verification | rollback | config | info",
      "action_type": "execute | verify | configure | rollback | observe",
      "description": "clear human-readable description of what this step does",
      "raw_text": "verbatim text of this step from the document",
      "commands": [
        {{
          "raw": "exact CLI command as in the document",
          "vendor": "cisco | juniper | nokia | arista | generic",
          "protocol": "bgp | ospf | isis | mpls | interface | vlan | system | null",
          "mode": "exec | config | null",
          "confidence": 0.95
        }}
      ],
      "expected_output": "what the operator should see, or null",
      "section": "section name from document heading, or null",
      "subsection": "sub-section name, or null",
      "is_rollback": false,
      "tags": ["tag1", "tag2"]
    }}
  ]
}}

---
DOCUMENT CONTENT:
{document_text}
---
"""


# ---------------------------------------------------------------------------
# Chunk prompt template — used when the document is split into chunks
# ---------------------------------------------------------------------------

_CHUNK_PROMPT_TEMPLATE = """\
Analyze the following SECTION of a larger MOP document and extract ALL actionable steps.

DOCUMENT TITLE: {title}
CHUNK: {chunk_index} of {total_chunks}
SECTIONS IN THIS CHUNK: {section_headings}
DETECTED STRUCTURE: {structure}
{cli_hint_section}

IMPORTANT CHUNKING INSTRUCTIONS:
- This is chunk {chunk_index} of {total_chunks} from a {total_chunks}-part document.
- Extract ONLY the steps from the content below. Do NOT invent steps from other parts.
- Use sequence numbers starting from {sequence_start} so they can be merged correctly.
- The "section" field for each step MUST reflect the actual heading in this chunk.

EXTRACTION RULES:
1. Extract EVERY step — do not skip, summarize, or merge steps.
2. Preserve CLI commands EXACTLY as written in the document.
3. If a step has multiple commands, list all of them in the "commands" array.
4. Classify each step type: action | verification | rollback | config | info
5. Classify action_type: execute | verify | configure | rollback | observe
6. For each CLI command detect: vendor, protocol, mode.
7. Extract expected_output if mentioned.
8. Set is_rollback: true for steps in Rollback/Backout/Recovery sections.
9. Add meaningful tags.

OUTPUT JSON SCHEMA (respond with ONLY this JSON, no other text):
{{
  "document_title": "string",
  "steps": [
    {{
      "sequence": {sequence_start},
      "step_type": "action | verification | rollback | config | info",
      "action_type": "execute | verify | configure | rollback | observe",
      "description": "clear human-readable description",
      "raw_text": "verbatim text of this step",
      "commands": [
        {{
          "raw": "exact CLI command",
          "vendor": "cisco | juniper | nokia | arista | generic",
          "protocol": "bgp | ospf | isis | mpls | interface | vlan | system | null",
          "mode": "exec | config | null",
          "confidence": 0.95
        }}
      ],
      "expected_output": "what the operator should see, or null",
      "section": "section name from heading, or null",
      "subsection": "sub-section name, or null",
      "is_rollback": false,
      "tags": ["tag1", "tag2"]
    }}
  ]
}}

---
CHUNK CONTENT:
{document_text}
---
"""


# ---------------------------------------------------------------------------
# Retry correction prompt — appended to conversation on JSON parse failure
# ---------------------------------------------------------------------------

JSON_CORRECTION_MESSAGE = """\
Your previous response was not valid JSON. This is attempt {attempt} of 3.

Requirements:
- Respond with ONLY the JSON object.
- Do NOT include markdown code fences (no ```json).
- Do NOT include any explanatory text before or after the JSON.
- The response must start with '{{' and end with '}}'.
- Ensure all strings are properly quoted and all brackets are closed.

Please retry now."""

SCHEMA_CORRECTION_MESSAGE = """\
Your previous response contained valid JSON but did not match the required schema.
This is attempt {attempt} of 3.

Schema violation details:
{validation_error}

Please fix the above and retry. Remember:
- Every step MUST have "sequence", "step_type", "action_type", "description", "raw_text".
- "commands" must be an array (can be empty []).
- "is_rollback" must be a boolean (true/false, not a string).
- Respond with ONLY the corrected JSON object."""


# ---------------------------------------------------------------------------
# Builder functions
# ---------------------------------------------------------------------------

def build_super_prompt(
    document_text: str,
    title: str,
    detected_structure: str = "unknown",
    pre_detected_commands: Optional[List[str]] = None,
) -> tuple[str, str]:
    """
    Build the (system_prompt, user_prompt) tuple for a full-document LLM call.

    Args:
        document_text:         Full text of the MOP document.
        title:                 Document title.
        detected_structure:    Structure hint from the normalizer.
        pre_detected_commands: CLI commands detected pre-LLM by grammar engine.

    Returns:
        (system_prompt, user_prompt) tuple.
    """
    cli_hint_section = _build_cli_hint_section(pre_detected_commands)

    user_prompt = _PROMPT_TEMPLATE.format(
        title=title,
        structure=detected_structure,
        cli_hint_section=cli_hint_section,
        document_text=document_text,
    )

    return SYSTEM_PROMPT, user_prompt


def build_chunk_prompt(
    chunk: DocumentChunk,
    title: str,
    detected_structure: str = "unknown",
    sequence_start: int = 1,
) -> tuple[str, str]:
    """
    Build the (system_prompt, user_prompt) tuple for a single document chunk.

    Args:
        chunk:              The DocumentChunk to process.
        title:              Document title (from ParsedDocument).
        detected_structure: Structure hint from the normalizer.
        sequence_start:     Step sequence number to start from (for global ordering).

    Returns:
        (system_prompt, user_prompt) tuple.
    """
    cli_hint_section = _build_cli_hint_section(chunk.pre_detected_commands)
    headings_str = " → ".join(chunk.section_headings) if chunk.section_headings else "Document"

    user_prompt = _CHUNK_PROMPT_TEMPLATE.format(
        title=title,
        chunk_index=chunk.chunk_index + 1,   # 1-based for human readability
        total_chunks=chunk.total_chunks,
        section_headings=headings_str,
        structure=detected_structure,
        cli_hint_section=cli_hint_section,
        sequence_start=sequence_start,
        document_text=chunk.text,
    )

    return SYSTEM_PROMPT, user_prompt


def _build_cli_hint_section(commands: Optional[List[str]]) -> str:
    """Build the CLI hints block to inject into any prompt variant."""
    if not commands:
        return ""
    cmd_list = "\n".join(f"  - {c}" for c in commands[:50])
    return (
        f"\nGRAMMAR ENGINE PRE-DETECTED CLI COMMANDS (ensure these are included):\n"
        f"{cmd_list}\n"
    )
