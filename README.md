
# MOP-Agnostic AI Test Generation Platform

## Overview

This platform converts any procedural document (MOP, SOP, or similar) into structured test artifacts, including:

- Zephyr test cases
- Robot Framework automation tests
- CLI validation rules

The platform is fully independent of pre/post phases and dynamically detects all actionable steps.

## Requirements

### Functional Requirements

1. Accept PDF/DOCX/TXT procedural documents.
2. Extract text while preserving bullet points, numbered lists, and tables.
3. Detect all actionable steps, including CLI commands, configuration steps, verification steps, and rollback steps.
4. Run a single Super Prompt LLM call to convert steps into structured JSON.
5. Enforce strict guardrails:
   - Pre-LLM command detection
   - Post-LLM command validation
   - JSON schema validation
   - Protocol and action classification
6. Generate outputs for:
   - Zephyr CSV bulk import
   - Robot Framework tests
   - CLI validation rules for compliance engines

### Non-Functional Requirements

1. Fast processing (<15 seconds per document).
2. Extensible for multiple network vendors (Cisco, Juniper, Nokia, Arista).
3. Reliable JSON outputs with schema validation.
4. Secure handling of documents; no credentials stored in parsing layer.

## Folder Structure

```
mop_ai_platform_repo/
│
├── ingestion/             # PDF/DOCX parsers
├── grammar_engine/        # CLI grammar extraction
├── ai_layer/              # Super Prompt LLM runner
├── post_processing/       # Guardrails and schema validation
├── generators/            # Zephyr/Robot/CLI generators
├── configs/               # YAML configs for protocols/patterns
├── tests/unit_tests/      # Unit tests for each component
```
