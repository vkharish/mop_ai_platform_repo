"""
MOP AI Platform — Main Pipeline

Orchestrates the full end-to-end flow:
  1. Load document (PDF/DOCX/TXT)
  2. Pre-LLM: grammar engine detects CLI commands as baseline
  2b. TOON build: compress structured docs 85-90% before LLM call
  3. LLM: super prompt extracts steps → CanonicalTestModel
  4. Post-LLM: guardrails validate extraction quality
  5. Schema validation
  6. Generate outputs: Zephyr CSV, Robot Framework, CLI Rules

Usage:
    python pipeline.py --input mop.pdf --output ./output
    python pipeline.py --input mop.docx --output ./output --title "BGP Migration MOP"
    python pipeline.py --input mop.txt --output ./output --model claude-opus-4-6
    python pipeline.py --input mop.pdf --output ./output --skip-toon
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pipeline")


def run(
    input_file: str,
    output_dir: str,
    title: str | None = None,
    model: str = "claude-sonnet-4-6",
    api_key: str | None = None,
    skip_guardrails: bool = False,
    skip_toon: bool = False,
) -> dict:
    """
    Run the full MOP → test artifacts pipeline.

    Args:
        input_file:      Path to the MOP document (PDF/DOCX/TXT).
        output_dir:      Directory for generated outputs.
        title:           Override document title (optional).
        model:           Claude model ID.
        api_key:         Anthropic API key (falls back to ANTHROPIC_API_KEY env var).
        skip_guardrails: Skip post-LLM guardrail checks (not recommended).
        skip_toon:       Disable TOON compression (send raw text to LLM).

    Returns:
        Dict with paths to generated output files and pipeline metadata.
    """
    start = time.time()

    # ------------------------------------------------------------------
    # 1. Ingest document
    # ------------------------------------------------------------------
    logger.info(f"[1/6] Loading document: {input_file}")
    from ingestion.document_loader import load as load_document
    doc = load_document(input_file)

    if title:
        doc.title = title

    logger.info(
        f"      Loaded '{doc.title}' ({doc.source_format.upper()}, "
        f"structure={doc.detected_structure}, "
        f"{len(doc.blocks)} blocks)"
    )

    # ------------------------------------------------------------------
    # 2. Pre-LLM: grammar engine baseline
    # ------------------------------------------------------------------
    logger.info("[2/6] Pre-LLM CLI command detection (grammar engine)")
    from grammar_engine.cli_grammar import CLIGrammar
    grammar = CLIGrammar()
    pre_commands = grammar.extract_from_text(doc.full_text)
    pre_command_strings = [cmd.raw for cmd in pre_commands]
    logger.info(f"      Detected {len(pre_commands)} CLI commands pre-LLM")

    # ------------------------------------------------------------------
    # 2b. TOON build (compress document before LLM call)
    # ------------------------------------------------------------------
    toon_doc = None
    if not skip_toon:
        logger.info("[2b] Building TOON (Tree of Outlined Nodes)")
        try:
            from toon.builder import TOONBuilder
            toon_doc = TOONBuilder.build(doc, grammar)
            if toon_doc.toon_usable:
                logger.info(
                    f"      TOON built: {toon_doc.compression_ratio:.1%} compression "
                    f"({toon_doc.estimated_raw_tokens:,} raw → "
                    f"{toon_doc.estimated_toon_tokens:,} TOON tokens), "
                    f"{len(toon_doc.sections)} sections"
                )
            else:
                logger.info(
                    f"      TOON not usable for this document "
                    f"({toon_doc.fallback_reason}); will use raw text"
                )
        except Exception as exc:
            logger.warning(f"      TOON build error (non-fatal): {exc}")
            toon_doc = None
    else:
        logger.info("[2b] TOON skipped (--skip-toon)")

    # ------------------------------------------------------------------
    # 3. LLM: super prompt extraction (with auto-chunking for large docs)
    # ------------------------------------------------------------------
    from ai_layer.context_chunker import ContextChunker
    chunker = ContextChunker()
    est_tokens = chunker.estimate_tokens(doc.full_text)
    needs_chunking = chunker.needs_chunking(doc)

    toon_active = (
        toon_doc is not None
        and toon_doc.toon_usable
        and not skip_toon
    )
    token_display = (
        f"{toon_doc.estimated_toon_tokens:,} TOON tokens"
        if toon_active
        else f"~{est_tokens:,} raw tokens"
    )
    logger.info(
        f"[3/6] Running LLM extraction (model={model}, "
        f"{token_display}, "
        f"{'TOON' if toon_active else ('chunked' if needs_chunking else 'single call')})"
    )

    from ai_layer.super_prompt_runner import SuperPromptRunner
    runner = SuperPromptRunner(model=model, api_key=api_key, use_toon=not skip_toon)
    llm_result = runner.run(
        doc,
        pre_detected_commands=pre_command_strings,
        toon_doc=toon_doc,
    )

    if not llm_result.success:
        raise RuntimeError(
            f"LLM extraction failed after {llm_result.attempt_count} attempt(s): "
            f"[{llm_result.error_type}] {llm_result.error_message}"
        )

    canonical_model = llm_result.model
    chunk_info = (
        f", {llm_result.chunk_count} chunks" if llm_result.chunk_count > 1 else ""
    )
    partial_info = (
        f" (PARTIAL — some chunks failed)" if llm_result.partial_steps > 0 else ""
    )
    logger.info(
        f"      Extracted {len(canonical_model.steps)} steps, "
        f"{sum(len(s.commands) for s in canonical_model.steps)} commands "
        f"in {llm_result.latency_ms / 1000:.1f}s"
        f"{chunk_info}{partial_info}"
    )
    if llm_result.partial_steps > 0:
        logger.warning(
            f"      WARNING: {canonical_model.metadata.get('warning', 'Some chunks failed.')}"
        )

    # ------------------------------------------------------------------
    # 4. Post-LLM guardrails
    # ------------------------------------------------------------------
    if not skip_guardrails:
        logger.info("[4/6] Running post-LLM guardrails")
        from post_processing.guardrails import Guardrails
        guardrail_result = Guardrails.validate(canonical_model, len(pre_commands))
        canonical_model.metadata["guardrails"] = {
            "passed": guardrail_result.passed,
            "warnings": guardrail_result.warnings,
            "errors": guardrail_result.errors,
            "coverage_ratio": guardrail_result.coverage_ratio,
        }
        if guardrail_result.warnings:
            for w in guardrail_result.warnings:
                logger.warning(f"      {w}")
        if not guardrail_result.passed:
            logger.error("      Guardrails FAILED — check errors above")
            for e in guardrail_result.errors:
                logger.error(f"      {e}")
    else:
        logger.info("[4/6] Guardrails skipped")

    # ------------------------------------------------------------------
    # 5. Schema validation
    # ------------------------------------------------------------------
    logger.info("[5/6] Schema validation")
    from post_processing.schema_validator import SchemaValidator
    validation = SchemaValidator.validate(canonical_model)
    if not validation.valid:
        logger.error("Schema validation FAILED:")
        for e in validation.errors:
            logger.error(f"  {e}")
        raise ValueError(f"Schema validation failed: {validation.errors}")
    else:
        logger.info("      Schema valid")

    # ------------------------------------------------------------------
    # 6. Generate outputs
    # ------------------------------------------------------------------
    logger.info(f"[6/6] Generating outputs → {output_dir}")
    from generators.zephyr_generator import ZephyrGenerator
    from generators.robot_generator import RobotGenerator
    from generators.cli_rule_generator import CLIRuleGenerator

    zephyr_path = ZephyrGenerator.generate(canonical_model, output_dir)
    robot_path = RobotGenerator.generate(canonical_model, output_dir)
    cli_rules_path = CLIRuleGenerator.generate(canonical_model, output_dir)

    # Also save the canonical JSON for debugging/auditing
    canonical_json_path = Path(output_dir) / f"{_safe_filename(canonical_model.document_title)}_canonical.json"
    with open(canonical_json_path, "w", encoding="utf-8") as f:
        f.write(SchemaValidator.to_json(canonical_model))

    elapsed = time.time() - start
    logger.info(f"Done in {elapsed:.1f}s")

    result = {
        "document_title": canonical_model.document_title,
        "source_file": input_file,
        "source_format": canonical_model.source_format,
        "mop_structure": canonical_model.mop_structure,
        "total_steps": len(canonical_model.steps),
        "toon": {
            "used": toon_active,
            "compression_ratio": (
                f"{toon_doc.compression_ratio:.1%}" if toon_doc else "n/a"
            ),
            "raw_tokens": toon_doc.estimated_raw_tokens if toon_doc else est_tokens,
            "toon_tokens": toon_doc.estimated_toon_tokens if toon_doc else 0,
        },
        "outputs": {
            "zephyr_csv": zephyr_path,
            "robot_framework": robot_path,
            "cli_rules": cli_rules_path,
            "canonical_json": str(canonical_json_path),
        },
        "metadata": canonical_model.metadata,
        "elapsed_seconds": round(elapsed, 2),
    }

    print(json.dumps(result, indent=2))
    return result


def _safe_filename(name: str) -> str:
    import re
    return re.sub(r"[^\w\-]", "_", name).strip("_")


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MOP AI Platform — Convert MOP documents to test artifacts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python pipeline.py --input mop.pdf --output ./output
  python pipeline.py --input mop.docx --output ./output --title "BGP Cutover MOP"
  python pipeline.py --input mop.txt --output ./output --model claude-opus-4-6
        """,
    )
    parser.add_argument("--input", "-i", required=True, help="Path to MOP document (PDF/DOCX/TXT)")
    parser.add_argument("--output", "-o", required=True, help="Output directory for generated files")
    parser.add_argument("--title", "-t", help="Override document title")
    parser.add_argument("--model", "-m", default="claude-sonnet-4-6", help="Claude model ID")
    parser.add_argument("--api-key", help="Anthropic API key (or set ANTHROPIC_API_KEY env var)")
    parser.add_argument("--skip-guardrails", action="store_true", help="Skip post-LLM guardrail checks")
    parser.add_argument("--skip-toon", action="store_true", help="Disable TOON compression (send raw text to LLM)")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    try:
        run(
            input_file=args.input,
            output_dir=args.output,
            title=args.title,
            model=args.model,
            api_key=args.api_key,
            skip_guardrails=args.skip_guardrails,
            skip_toon=args.skip_toon,
        )
    except Exception as e:
        logger.error(f"Pipeline failed: {e}")
        sys.exit(1)
