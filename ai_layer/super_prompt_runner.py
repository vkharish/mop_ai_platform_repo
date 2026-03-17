"""
Super Prompt Runner — LLM Orchestration Layer

Converts a ParsedDocument into a CanonicalTestModel via Claude API.

Features:
  - LLMResult typed return — never raises, always returns a result
  - TOON pre-processing: 85-90% token reduction for structured documents
  - 3-attempt retry per call with conversation history for JSON/schema failures
  - Exponential backoff for rate limits
  - Automatic context chunking for large documents (50+ pages)
  - Chunk merging with globally renumbered step sequences
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import anthropic

from ai_layer.context_chunker import ContextChunker, DocumentChunk
from ai_layer.llm_result import LLMErrorType, LLMResult
from ai_layer.prompts.super_prompt import (
    JSON_CORRECTION_MESSAGE,
    SCHEMA_CORRECTION_MESSAGE,
    build_chunk_prompt,
    build_super_prompt,
)
from ai_layer.prompts.toon_prompt import (
    TOON_JSON_CORRECTION_MESSAGE,
    TOON_SCHEMA_CORRECTION_MESSAGE,
    build_toon_chunk_prompt,
    build_toon_prompt,
)
from models.canonical import (
    ActionType,
    CanonicalTestModel,
    CLICommand,
    ParsedDocument,
    StepType,
    TestStep,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8192
MAX_ATTEMPTS = 3

# Backoff delays (seconds) for rate limit retries
_RATE_LIMIT_BACKOFF = [2, 4, 8]


class SuperPromptRunner:
    """
    Orchestrates LLM calls to convert a MOP document into a CanonicalTestModel.

    Returns LLMResult — check .success before accessing .model.
    Never raises (unless the Anthropic client itself cannot be instantiated).
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens_per_chunk: int = 80_000,
        use_toon: bool = True,
    ):
        self._model = model
        self._temperature = temperature
        self._chunker = ContextChunker(max_tokens_per_chunk=max_tokens_per_chunk)
        self._use_toon = use_toon
        self._client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        doc: ParsedDocument,
        pre_detected_commands: Optional[List[str]] = None,
        toon_doc=None,
    ) -> LLMResult:
        """
        Convert a ParsedDocument into a CanonicalTestModel.

        Automatically uses TOON compression when available, then chunks if still
        too large.  Falls back to raw text for prose/unknown documents.
        Returns LLMResult — never raises.

        Args:
            doc:                    ParsedDocument from the ingestion layer.
            pre_detected_commands:  Pre-LLM CLI commands from grammar engine.
            toon_doc:               Optional TOONDocument (pre-built by pipeline).
                                    If None and use_toon=True, builds it here.

        Returns:
            LLMResult with success=True and .model set, or success=False with
            error_type and error_message explaining the failure.
        """
        commands = pre_detected_commands or []

        # --- TOON path ---
        if self._use_toon:
            if toon_doc is None:
                toon_doc = self._build_toon(doc)

            if toon_doc is not None and toon_doc.toon_usable:
                logger.info(
                    f"TOON active: {toon_doc.compression_ratio:.1%} compression "
                    f"({toon_doc.estimated_raw_tokens:,} → "
                    f"{toon_doc.estimated_toon_tokens:,} tokens)"
                )
                return self._run_toon(doc, toon_doc, commands)

        # --- Raw text path (prose / unknown structure) ---
        if self._chunker.needs_chunking(doc):
            return self._run_chunked(doc, commands)
        return self._run_single(doc, commands)

    @staticmethod
    def _build_toon(doc: ParsedDocument):
        """Build a TOONDocument from a ParsedDocument (lazy import)."""
        try:
            from toon.builder import TOONBuilder
            from grammar_engine.cli_grammar import CLIGrammar
            grammar = CLIGrammar()
            return TOONBuilder.build(doc, grammar)
        except Exception as e:
            logger.warning(f"TOON build failed, falling back to raw text: {e}")
            return None

    # ------------------------------------------------------------------
    # TOON path
    # ------------------------------------------------------------------

    def _run_toon(
        self,
        doc: ParsedDocument,
        toon_doc,
        pre_detected_commands: List[str],
    ) -> LLMResult:
        """Run using TOON-compressed text. Chunks if TOON is still too large."""
        from toon.renderer import TOONRenderer

        # Check if TOON itself needs chunking
        toon_text = TOONRenderer.render(toon_doc)
        toon_tokens = toon_doc.estimated_toon_tokens

        if toon_tokens > self._chunker._max_tokens:
            logger.info(
                f"TOON ({toon_tokens:,} tokens) still exceeds budget "
                f"({self._chunker._max_tokens:,}), chunking TOON sections"
            )
            return self._run_toon_chunked(doc, toon_doc, pre_detected_commands)

        system_prompt, user_prompt = build_toon_prompt(
            toon_doc=toon_doc,
            pre_detected_commands=pre_detected_commands or toon_doc.all_commands[:50],
        )
        result = self._call_with_retry(
            system_prompt, user_prompt, doc,
            sequence_start=1,
            is_toon=True,
        )

        if result.success and result.model:
            result.model.metadata.update({
                "toon_used": True,
                "toon_compression_ratio": toon_doc.compression_ratio,
                "toon_raw_tokens": toon_doc.estimated_raw_tokens,
                "toon_tokens": toon_doc.estimated_toon_tokens,
                "chunks_processed": 1,
                "total_steps": len(result.model.steps),
            })

        return result

    def _run_toon_chunked(
        self,
        doc: ParsedDocument,
        toon_doc,
        pre_detected_commands: List[str],
    ) -> LLMResult:
        """Chunk a large TOONDocument section-by-section."""
        from toon.renderer import TOONRenderer

        # Group TOON sections into token-budget chunks
        section_chunks = self._pack_toon_sections(toon_doc)
        total_chunks = len(section_chunks)
        logger.info(f"TOON chunked into {total_chunks} section groups")

        successful_models: List[CanonicalTestModel] = []
        failed_chunks: List[int] = []
        total_latency_ms = 0
        total_attempts = 0
        sequence_cursor = 1

        for idx, section_group in enumerate(section_chunks):
            headings = [s.heading for s in section_group]
            chunk_text = "\n".join(
                TOONRenderer.render_section_only(s) for s in section_group
            )
            chunk_commands = [
                cmd
                for s in section_group
                for node in s.nodes
                for cmd in node.commands
            ]

            system_prompt, user_prompt = build_toon_chunk_prompt(
                toon_text=chunk_text,
                title=toon_doc.title,
                section_headings=headings,
                chunk_index=idx,
                total_chunks=total_chunks,
                sequence_start=sequence_cursor,
                pre_detected_commands=chunk_commands or pre_detected_commands,
            )

            chunk_result = self._call_with_retry(
                system_prompt, user_prompt, doc,
                sequence_start=sequence_cursor,
                is_toon=True,
            )
            total_latency_ms += chunk_result.latency_ms
            total_attempts += chunk_result.attempt_count

            if chunk_result.success and chunk_result.model:
                successful_models.append(chunk_result.model)
                sequence_cursor += len(chunk_result.model.steps)
                logger.info(f"  ✓ TOON chunk {idx+1}: {len(chunk_result.model.steps)} steps")
            else:
                failed_chunks.append(idx + 1)
                logger.warning(f"  ✗ TOON chunk {idx+1} failed: {chunk_result.error_message}")

        if not successful_models:
            return LLMResult(
                success=False,
                error_type=LLMErrorType.UNKNOWN,
                error_message=f"All {total_chunks} TOON chunks failed.",
                latency_ms=total_latency_ms,
                attempt_count=total_attempts,
                chunk_count=total_chunks,
            )

        merged = self._merge_chunk_models(successful_models, doc, total_latency_ms)
        merged.metadata.update({
            "toon_used": True,
            "toon_compression_ratio": toon_doc.compression_ratio,
        })
        if failed_chunks:
            merged.metadata["failed_chunks"] = failed_chunks

        return LLMResult(
            success=True,
            model=merged,
            latency_ms=total_latency_ms,
            attempt_count=total_attempts,
            chunk_count=total_chunks,
            partial_steps=len(merged.steps) if failed_chunks else 0,
        )

    def _pack_toon_sections(self, toon_doc) -> List[List]:
        """Greedy-bin-pack TOONSections into token-budget groups."""
        from toon.renderer import TOONRenderer

        groups: List[List] = []
        current_group: List = []
        current_tokens = 0

        for section in toon_doc.sections:
            section_text = TOONRenderer.render_section_only(section)
            section_tokens = max(1, int(len(section_text) / 3.5))

            if current_group and current_tokens + section_tokens > self._chunker._max_tokens:
                groups.append(current_group)
                current_group = []
                current_tokens = 0

            current_group.append(section)
            current_tokens += section_tokens

        if current_group:
            groups.append(current_group)

        return groups

    # ------------------------------------------------------------------
    # Single-document path (no chunking)
    # ------------------------------------------------------------------

    def _run_single(
        self,
        doc: ParsedDocument,
        pre_detected_commands: List[str],
    ) -> LLMResult:
        system_prompt, user_prompt = build_super_prompt(
            document_text=doc.full_text,
            title=doc.title,
            detected_structure=doc.detected_structure,
            pre_detected_commands=pre_detected_commands,
        )

        result = self._call_with_retry(system_prompt, user_prompt, doc, sequence_start=1)

        if result.success and result.model:
            result.model.metadata.update({
                "chunks_processed": 1,
                "total_steps": len(result.model.steps),
                "total_commands": sum(len(s.commands) for s in result.model.steps),
            })

        return result

    # ------------------------------------------------------------------
    # Chunked path (large documents)
    # ------------------------------------------------------------------

    def _run_chunked(
        self,
        doc: ParsedDocument,
        pre_detected_commands: List[str],
    ) -> LLMResult:
        chunks = self._chunker.chunk(doc, pre_detected_commands)
        logger.info(
            f"Document split into {len(chunks)} chunks "
            f"(est. {self._chunker.estimate_tokens(doc.full_text):,} tokens total)"
        )

        successful_models: List[CanonicalTestModel] = []
        failed_chunks: List[int] = []
        total_latency_ms = 0
        total_attempts = 0
        sequence_cursor = 1  # global sequence counter across chunks

        for chunk in chunks:
            logger.info(
                f"  Processing chunk {chunk.chunk_index + 1}/{chunk.total_chunks} "
                f"[{', '.join(chunk.section_headings) or 'preamble'}] "
                f"~{chunk.estimated_tokens:,} tokens"
            )
            system_prompt, user_prompt = build_chunk_prompt(
                chunk=chunk,
                title=doc.title,
                detected_structure=doc.detected_structure,
                sequence_start=sequence_cursor,
            )

            chunk_result = self._call_with_retry(
                system_prompt, user_prompt, doc, sequence_start=sequence_cursor
            )
            total_latency_ms += chunk_result.latency_ms
            total_attempts += chunk_result.attempt_count

            if chunk_result.success and chunk_result.model:
                successful_models.append(chunk_result.model)
                sequence_cursor += len(chunk_result.model.steps)
                logger.info(
                    f"    ✓ Chunk {chunk.chunk_index + 1}: "
                    f"{len(chunk_result.model.steps)} steps extracted"
                )
            else:
                failed_chunks.append(chunk.chunk_index + 1)
                logger.warning(
                    f"    ✗ Chunk {chunk.chunk_index + 1} failed: "
                    f"[{chunk_result.error_type}] {chunk_result.error_message}"
                )

        # Decide overall success
        if not successful_models:
            return LLMResult(
                success=False,
                error_type=LLMErrorType.UNKNOWN,
                error_message=f"All {len(chunks)} chunks failed. "
                              f"Check individual chunk errors above.",
                latency_ms=total_latency_ms,
                attempt_count=total_attempts,
                chunk_count=len(chunks),
            )

        merged = self._merge_chunk_models(successful_models, doc, total_latency_ms)
        partial = len(failed_chunks) > 0

        if partial:
            merged.metadata["failed_chunks"] = failed_chunks
            merged.metadata["warning"] = (
                f"Chunks {failed_chunks} failed — {len(failed_chunks)} section(s) "
                f"may be missing from the output."
            )
            logger.warning(merged.metadata["warning"])

        return LLMResult(
            success=True,
            model=merged,
            latency_ms=total_latency_ms,
            attempt_count=total_attempts,
            chunk_count=len(chunks),
            partial_steps=len(merged.steps) if partial else 0,
        )

    # ------------------------------------------------------------------
    # Core LLM call with retry
    # ------------------------------------------------------------------

    def _call_with_retry(
        self,
        system_prompt: str,
        user_prompt: str,
        doc: ParsedDocument,
        sequence_start: int = 1,
        is_toon: bool = False,
    ) -> LLMResult:
        """
        Call the LLM with up to MAX_ATTEMPTS retries.

        Retry strategy:
          JSON_PARSE_FAIL   → add correction message to conversation, retry
          SCHEMA_VIOLATION  → add schema error to conversation, retry
          RATE_LIMIT        → exponential backoff, retry with fresh messages
          CONTEXT_TOO_LONG  → non-retryable (chunk smaller)
          REFUSAL           → non-retryable
        """
        messages: List[Dict] = [{"role": "user", "content": user_prompt}]
        total_latency_ms = 0
        last_result: Optional[LLMResult] = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            start_ms = int(time.time() * 1000)

            try:
                raw = self._call_api(system_prompt, messages)
            except anthropic.RateLimitError:
                delay = _RATE_LIMIT_BACKOFF[min(attempt - 1, len(_RATE_LIMIT_BACKOFF) - 1)]
                logger.warning(f"Rate limit hit (attempt {attempt}), backing off {delay}s")
                time.sleep(delay)
                elapsed_ms = int(time.time() * 1000) - start_ms
                total_latency_ms += elapsed_ms
                last_result = LLMResult(
                    success=False,
                    error_type=LLMErrorType.RATE_LIMIT,
                    error_message="Anthropic rate limit",
                    latency_ms=total_latency_ms,
                    attempt_count=attempt,
                )
                # Reset conversation for rate limit retry
                messages = [{"role": "user", "content": user_prompt}]
                continue
            except anthropic.BadRequestError as e:
                if "context" in str(e).lower() or "too long" in str(e).lower():
                    return LLMResult(
                        success=False,
                        error_type=LLMErrorType.CONTEXT_TOO_LONG,
                        error_message=str(e),
                        latency_ms=total_latency_ms,
                        attempt_count=attempt,
                    )
                return LLMResult(
                    success=False,
                    error_type=LLMErrorType.UNKNOWN,
                    error_message=str(e),
                    latency_ms=total_latency_ms,
                    attempt_count=attempt,
                )
            except Exception as e:
                return LLMResult(
                    success=False,
                    error_type=LLMErrorType.UNKNOWN,
                    error_message=str(e),
                    latency_ms=total_latency_ms,
                    attempt_count=attempt,
                )

            elapsed_ms = int(time.time() * 1000) - start_ms
            total_latency_ms += elapsed_ms

            # Try to parse the response
            parse_result = self._parse_response(raw, doc, sequence_start)

            if parse_result.success:
                parse_result.latency_ms = total_latency_ms
                parse_result.attempt_count = attempt
                return parse_result

            last_result = parse_result
            last_result.latency_ms = total_latency_ms
            last_result.attempt_count = attempt

            if attempt >= MAX_ATTEMPTS:
                break

            # Non-retryable errors
            if parse_result.error_type in (LLMErrorType.CONTEXT_TOO_LONG, LLMErrorType.REFUSAL):
                break

            # Build correction message for next attempt
            correction = self._build_correction_message(parse_result, attempt + 1, is_toon=is_toon)
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": correction})
            logger.warning(
                f"Attempt {attempt} failed [{parse_result.error_type}], retrying..."
            )

        return last_result or LLMResult(
            success=False,
            error_type=LLMErrorType.UNKNOWN,
            error_message="All attempts exhausted",
            latency_ms=total_latency_ms,
            attempt_count=MAX_ATTEMPTS,
        )

    def _call_api(self, system_prompt: str, messages: List[Dict]) -> str:
        """Make the raw Anthropic API call."""
        message = self._client.messages.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            temperature=self._temperature,
            system=system_prompt,
            messages=messages,
        )
        return message.content[0].text

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(
        self,
        raw: str,
        doc: ParsedDocument,
        sequence_start: int,
    ) -> LLMResult:
        """Parse raw LLM text into an LLMResult."""
        # Check for refusal patterns
        if self._looks_like_refusal(raw):
            return LLMResult(
                success=False,
                error_type=LLMErrorType.REFUSAL,
                error_message="LLM declined to process the document.",
                raw_response=raw[:500],
            )

        # Extract JSON
        parsed_json, parse_error = self._extract_json(raw)
        if parsed_json is None:
            return LLMResult(
                success=False,
                error_type=LLMErrorType.JSON_PARSE_FAIL,
                error_message=parse_error,
                raw_response=raw[:1000],
            )

        # Build canonical model
        try:
            model = self._build_canonical_model(parsed_json, doc, sequence_start)
        except Exception as e:
            return LLMResult(
                success=False,
                error_type=LLMErrorType.SCHEMA_VIOLATION,
                error_message=str(e),
                raw_response=raw[:1000],
            )

        return LLMResult(success=True, model=model)

    def _extract_json(self, raw: str) -> Tuple[Optional[Dict], str]:
        """
        Extract JSON from raw LLM response using three strategies.

        Returns (parsed_dict, error_message). On success, error_message is "".
        """
        # Strategy 1: strip markdown fences, try direct parse
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned.strip())
        try:
            return json.loads(cleaned), ""
        except json.JSONDecodeError:
            pass

        # Strategy 2: find outermost { ... } block
        match = re.search(r"\{[\s\S]+\}", cleaned)
        if match:
            try:
                return json.loads(match.group(0)), ""
            except json.JSONDecodeError:
                pass

        # Strategy 3: try the raw response directly
        try:
            return json.loads(raw), ""
        except json.JSONDecodeError as e:
            return None, f"JSON parse failed: {e}. Response starts with: {raw[:200]!r}"

    def _build_canonical_model(
        self,
        llm_json: Dict[str, Any],
        doc: ParsedDocument,
        sequence_start: int,
    ) -> CanonicalTestModel:
        """Convert the LLM JSON dict into a CanonicalTestModel."""
        raw_steps = llm_json.get("steps", [])
        steps = []

        for i, raw_step in enumerate(raw_steps):
            commands = [
                CLICommand(
                    raw=cmd.get("raw", ""),
                    vendor=cmd.get("vendor") or "generic",
                    protocol=cmd.get("protocol"),
                    mode=cmd.get("mode"),
                    confidence=float(cmd.get("confidence", 0.9)),
                )
                for cmd in raw_step.get("commands", [])
                if cmd.get("raw", "").strip()
            ]

            # Use LLM-provided sequence if present, otherwise assign from start
            seq = raw_step.get("sequence")
            if seq is None or not isinstance(seq, int):
                seq = sequence_start + i

            steps.append(TestStep(
                step_id=str(uuid.uuid4())[:8],
                sequence=seq,
                step_type=self._safe_enum(StepType, raw_step.get("step_type"), StepType.ACTION),
                action_type=self._safe_enum(ActionType, raw_step.get("action_type"), ActionType.EXECUTE),
                description=raw_step.get("description", ""),
                raw_text=raw_step.get("raw_text", ""),
                commands=commands,
                expected_output=raw_step.get("expected_output"),
                section=raw_step.get("section"),
                subsection=raw_step.get("subsection"),
                is_rollback=bool(raw_step.get("is_rollback", False)),
                tags=raw_step.get("tags", []),
            ))

        return CanonicalTestModel(
            document_title=llm_json.get("document_title") or doc.title,
            source_file=doc.source_file,
            source_format=doc.source_format,
            mop_structure=doc.detected_structure,
            steps=steps,
            metadata={"llm_model": self._model},
        )

    # ------------------------------------------------------------------
    # Chunk merging
    # ------------------------------------------------------------------

    def _merge_chunk_models(
        self,
        models: List[CanonicalTestModel],
        doc: ParsedDocument,
        total_latency_ms: int,
    ) -> CanonicalTestModel:
        """
        Merge multiple per-chunk CanonicalTestModels into one.

        Steps are concatenated in chunk order and globally renumbered
        (1, 2, 3, ...) so the final model has a clean, contiguous sequence.
        """
        all_steps: List[TestStep] = []
        for model in models:
            for step in model.steps:
                # Assign globally unique sequence (1-based)
                step.sequence = len(all_steps) + 1
                all_steps.append(step)

        total_commands = sum(len(s.commands) for s in all_steps)

        return CanonicalTestModel(
            document_title=models[0].document_title,
            source_file=doc.source_file,
            source_format=doc.source_format,
            mop_structure=doc.detected_structure,
            steps=all_steps,
            metadata={
                "llm_model": self._model,
                "llm_elapsed_ms": total_latency_ms,
                "llm_elapsed_seconds": round(total_latency_ms / 1000, 2),
                "total_steps": len(all_steps),
                "total_commands": total_commands,
                "chunks_processed": len(models),
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_correction_message(
        self, result: LLMResult, attempt: int, is_toon: bool = False
    ) -> str:
        if result.error_type == LLMErrorType.JSON_PARSE_FAIL:
            template = TOON_JSON_CORRECTION_MESSAGE if is_toon else JSON_CORRECTION_MESSAGE
            return template.format(attempt=attempt)
        if result.error_type == LLMErrorType.SCHEMA_VIOLATION:
            template = TOON_SCHEMA_CORRECTION_MESSAGE if is_toon else SCHEMA_CORRECTION_MESSAGE
            return template.format(
                attempt=attempt,
                validation_error=result.error_message[:500],
            )
        template = TOON_JSON_CORRECTION_MESSAGE if is_toon else JSON_CORRECTION_MESSAGE
        return template.format(attempt=attempt)

    @staticmethod
    def _looks_like_refusal(text: str) -> bool:
        """
        Heuristic check for genuine LLM refusal responses.

        A refusal has two properties:
          1. No JSON content anywhere in the response (no '{' character)
          2. Contains specific refusal phrasing — deliberately narrow to avoid
             false positives on error messages that happen to say "cannot"
        """
        if "{" in text:
            return False  # response contains JSON-like content → not a refusal
        refusal_phrases = [
            "i'm unable to process this request",
            "i cannot process this request",
            "i cannot assist with this",
            "i can't assist with this",
            "i won't process",
            "i must decline",
            "i am unable to assist",
            "not appropriate for me to",
            "i'm not able to help with this",
        ]
        lower = text.lower()[:300]
        return any(phrase in lower for phrase in refusal_phrases)

    @staticmethod
    def _safe_enum(enum_cls, value: Any, default):
        try:
            return enum_cls(value)
        except (ValueError, KeyError):
            return default
