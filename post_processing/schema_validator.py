"""
Schema Validator

Validates a CanonicalTestModel against the expected JSON schema
before it is passed to generators.

Uses Pydantic's built-in validation for the main model,
then applies additional business rules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List

from models.canonical import CanonicalTestModel


@dataclass
class ValidationResult:
    valid: bool
    errors: List[str] = field(default_factory=list)


class SchemaValidator:
    """Validates CanonicalTestModel structure and business rules."""

    @classmethod
    def validate(cls, model: CanonicalTestModel) -> ValidationResult:
        """
        Run schema and business-rule validation.

        Returns:
            ValidationResult with valid flag and list of errors.
        """
        result = ValidationResult(valid=True)

        cls._validate_pydantic(model, result)
        cls._validate_sequences_ordered(model, result)
        cls._validate_document_title(model, result)
        cls._validate_source_format(model, result)

        if result.errors:
            result.valid = False

        return result

    @classmethod
    def _validate_pydantic(cls, model: CanonicalTestModel, result: ValidationResult):
        """Re-validate via Pydantic (catches type errors)."""
        try:
            # Round-trip through JSON to force full Pydantic validation
            CanonicalTestModel.model_validate_json(model.model_dump_json())
        except Exception as e:
            result.errors.append(f"Schema validation error: {e}")

    @classmethod
    def _validate_sequences_ordered(cls, model: CanonicalTestModel, result: ValidationResult):
        sequences = [s.sequence for s in model.steps]
        if sequences != sorted(sequences):
            result.errors.append(
                "Step sequences are not in ascending order. "
                f"Found: {sequences}"
            )

    @classmethod
    def _validate_document_title(cls, model: CanonicalTestModel, result: ValidationResult):
        if not model.document_title or not model.document_title.strip():
            result.errors.append("document_title is empty or missing.")

    @classmethod
    def _validate_source_format(cls, model: CanonicalTestModel, result: ValidationResult):
        allowed = {"pdf", "docx", "txt"}
        if model.source_format not in allowed:
            result.errors.append(
                f"source_format '{model.source_format}' is not in allowed set {allowed}."
            )

    @classmethod
    def to_json(cls, model: CanonicalTestModel, indent: int = 2) -> str:
        """Serialize the canonical model to a pretty JSON string."""
        return model.model_dump_json(indent=indent)
