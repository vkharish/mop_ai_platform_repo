"""
LLMResult — Typed wrapper for LLM call outcomes.

Every call to the LLM returns an LLMResult instead of raising exceptions.
The caller (SuperPromptRunner) decides whether to retry, degrade gracefully,
or propagate a failure based on the error_type.

Error type semantics:
  JSON_PARSE_FAIL    — response text is not valid JSON (LLM added prose, cut off, etc.)
  SCHEMA_VIOLATION   — valid JSON but doesn't match CanonicalTestModel schema
  RATE_LIMIT         — Anthropic API rate limit / overload (retry with backoff)
  CONTEXT_TOO_LONG   — prompt + doc exceeded model context window (chunk smaller)
  REFUSAL            — model declined to process the content
  UNKNOWN            — unclassified error (inspect error_message for details)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from models.canonical import CanonicalTestModel


class LLMErrorType(str, Enum):
    JSON_PARSE_FAIL = "json_parse_fail"
    SCHEMA_VIOLATION = "schema_violation"
    RATE_LIMIT = "rate_limit"
    CONTEXT_TOO_LONG = "context_too_long"
    REFUSAL = "refusal"
    UNKNOWN = "unknown"


@dataclass
class LLMResult:
    """
    Result of a single LLM call (or a merged multi-chunk call).

    Always check `success` before accessing `model`.
    """

    success: bool

    model: Optional[CanonicalTestModel] = None
    """Populated on success. Contains the extracted CanonicalTestModel."""

    error_type: Optional[LLMErrorType] = None
    """Populated on failure. Classifies what went wrong."""

    error_message: str = ""
    """Human-readable error detail for logging / debugging."""

    raw_response: str = ""
    """Last raw LLM response text (for debugging failed parses)."""

    latency_ms: int = 0
    """Total LLM wall-clock time in milliseconds (summed across retries)."""

    attempt_count: int = 1
    """How many attempts were made before this result."""

    chunk_count: int = 1
    """Number of chunks this result was assembled from (1 = no chunking)."""

    partial_steps: int = 0
    """
    Number of steps recovered from successful chunks when some chunks failed.
    0 means either full success or total failure.
    """

    def raise_if_failed(self) -> CanonicalTestModel:
        """
        Convenience method: return the model on success, raise on failure.
        Use this when the caller cannot handle partial results.
        """
        if self.success and self.model is not None:
            return self.model
        raise LLMError(
            f"LLM extraction failed after {self.attempt_count} attempt(s): "
            f"[{self.error_type}] {self.error_message}"
        )


class LLMError(Exception):
    """Raised by LLMResult.raise_if_failed() when extraction failed."""
    pass
