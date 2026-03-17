"""
Base Normalizer — abstract interface for MOP structure normalizers.

All normalizers follow the Strategy pattern: they receive a list of
DocumentBlocks and produce a flat ordered list of step texts.

NOTE: Normalizers are structural helpers for the ingestion layer only.
The LLM (super_prompt_runner) is the authoritative step extractor.
Normalizer output is used only to produce better-structured LLM input.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from models.canonical import DocumentBlock


class BaseNormalizer(ABC):
    """
    Abstract base for MOP structure normalizers.

    Subclasses implement `can_handle` to declare suitability and
    `extract_steps` to produce ordered step strings from blocks.
    """

    @classmethod
    @abstractmethod
    def can_handle(cls, blocks: List[DocumentBlock]) -> bool:
        """Return True if this normalizer is suitable for the given blocks."""
        ...

    @classmethod
    @abstractmethod
    def extract_steps(cls, blocks: List[DocumentBlock]) -> List[str]:
        """
        Extract ordered step strings from the blocks.

        Returns a list of plain-text step descriptions (no structure markup).
        The LLM will receive these as input.
        """
        ...
